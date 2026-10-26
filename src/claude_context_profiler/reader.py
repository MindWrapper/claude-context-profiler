"""Read a recording: load the files that the proxy wrote and pair each request
with its response.

Runs offline, over the run directory. Nothing here touches the network or a
live session, so it is free to be slow and thorough.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
from pathlib import Path

MESSAGES_PATH = "/v1/messages"


# --- reading the recording ------------------------------------------------


# An escaped UTF-16 surrogate. The proxy escapes a record only when it holds a lone one.
SURROGATE_RE = re.compile(r"\\u[dD][89a-fA-F]")


def whole_characters(value):
    """A copy with each lone surrogate replaced by U+FFFD, which UTF-8 can write."""
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {k: whole_characters(v) for k, v in value.items()}
    if isinstance(value, list):
        return [whole_characters(v) for v in value]
    return value


def load_run(run_dir: Path) -> list[dict]:
    records = []
    for path in sorted(run_dir.glob("[0-9]*.json")):
        try:
            text = path.read_text(encoding="utf-8")
            record = json.loads(text)
        except json.JSONDecodeError:
            print(f"reader: skipping unreadable {path.name}", file=sys.stderr)
            continue
        records.append(whole_characters(record) if SURROGATE_RE.search(text) else record)
    records.sort(key=lambda r: r.get("index", 0))
    return records


def is_messages_call(record: dict) -> bool:
    """A request to the model. The exact path, so /v1/messages/count_tokens, which Claude Code calls
    to count tokens (for /context, say), is not one."""
    path = record.get("path", "").split("?", 1)[0]
    return record.get("method") == "POST" and path == MESSAGES_PATH and "request_json" in record


def session_id(record: dict) -> str | None:
    """Claude Code's session ID, from the header it sends, or else from the request's metadata."""
    header = (record.get("request_headers") or {}).get("x-claude-code-session-id")
    if header:
        return header
    user_id = ((record.get("request_json") or {}).get("metadata") or {}).get("user_id") or ""
    try:
        return json.loads(user_id).get("session_id")
    except (ValueError, AttributeError):
        # An older Claude Code writes user_id as "user_…_account_…_session_<id>".
        return user_id.rpartition("_session_")[2] or None


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        payload = "".join(line[6:] for line in block.splitlines() if line.startswith("data: "))
        if not payload:
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            pass
    return events


def reassemble(record: dict) -> dict:
    """Rebuild the answer from the stream, or read it from a plain response."""
    if "response_json" in record:
        body = record["response_json"]
        return {
            "blocks": body.get("content") or [],
            "usage": body.get("usage") or {},
            "stop_reason": body.get("stop_reason"),
            "error": body.get("error"),
        }

    events = parse_sse(record.get("response_sse") or "")
    blocks: dict[int, dict] = {}
    usage: dict = {}
    stop_reason = None
    error = None
    for event in events:
        kind = event.get("type")
        if kind == "message_start":
            usage.update((event.get("message") or {}).get("usage") or {})
        elif kind == "content_block_start":
            blocks[event.get("index", len(blocks))] = dict(event.get("content_block") or {})
        elif kind == "content_block_delta":
            block = blocks.setdefault(event.get("index", 0), {"type": "text"})
            delta = event.get("delta") or {}
            for field, target in (("text", "text"), ("thinking", "thinking"), ("partial_json", "partial_json")):
                if field in delta:
                    block[target] = (block.get(target) or "") + delta[field]
        elif kind == "message_delta":
            usage.update(event.get("usage") or {})
            stop_reason = (event.get("delta") or {}).get("stop_reason", stop_reason)
        elif kind == "error":
            error = event.get("error")
    return {
        "blocks": [blocks[i] for i in sorted(blocks)],
        "usage": usage,
        "stop_reason": stop_reason,
        "error": error,
    }


# --- shaping the request --------------------------------------------------


def strip_cache_control(value):
    """A copy without cache_control, for comparing content across requests."""
    if isinstance(value, dict):
        return {k: strip_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache_control(v) for v in value]
    return value


def canon(value) -> str:
    return json.dumps(strip_cache_control(value), sort_keys=True, ensure_ascii=False)


def system_blocks(body: dict) -> list[dict]:
    system = body.get("system")
    if system is None:
        return []
    if isinstance(system, str):
        return [{"type": "text", "text": system}]
    return system


def block_text(block) -> str:
    if isinstance(block, str):
        return block
    for field in ("text", "thinking", "partial_json"):
        if block.get(field):
            return block[field]
    if block.get("type") == "tool_use":
        return json.dumps(block.get("input") or {}, indent=2, ensure_ascii=False)
    if block.get("type") == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            return "\n".join(block_text(part) for part in content)
        return content if isinstance(content, str) else json.dumps(content, indent=2, ensure_ascii=False)
    return json.dumps(block, indent=2, ensure_ascii=False)


def chain_key(body: dict) -> str:
    """Requests of one conversation share their first message.

    A subagent, a compaction and the quota probe each open their own first
    message, so they land in their own chain instead of being mixed into
    the main conversation.
    """
    messages = body.get("messages") or []
    seed = canon(messages[0]) if messages else canon(system_blocks(body)[:1])
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


def breakpoints(body: dict) -> list[str]:
    """Where the request asks the API to cut a prompt-cache segment."""
    marks = []
    for i, block in enumerate(system_blocks(body)):
        if isinstance(block, dict) and block.get("cache_control"):
            marks.append(f"system block {i}")
    tools = body.get("tools") or []
    for i, tool in enumerate(tools):
        if tool.get("cache_control"):
            marks.append(f"tool {i} ({tool.get('name', '?')}), last of {len(tools)}")
    for i, message in enumerate(body.get("messages") or []):
        content = message.get("content")
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("cache_control"):
                    marks.append(f"message {i} block {j} ({message.get('role')})")
    return marks


def common_prefix(previous: list, current: list) -> int:
    n = 0
    for a, b in zip(previous, current):
        # Equal as they are is the common case and cheap; canon() is for a moved cache_control.
        if a != b and canon(a) != canon(b):
            break
        n += 1
    return n


def usage_row(usage: dict) -> dict:
    fresh = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    return {
        "prompt_in": fresh + cache_read + cache_write,
        "fresh": fresh,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": usage.get("output_tokens", 0) or 0,
    }


def build_exchanges(records: list[dict]) -> list[dict]:
    """One entry per API call, with its diff against the previous call."""
    exchanges = []
    previous_by_chain: dict[str, dict] = {}
    chain_numbers: dict[str, int] = {}
    current: dict[str, str] = {}  # first message's key -> the chain it leads to now

    for record in records:
        body = record["request_json"]
        first = chain_key(body)
        key = current.get(first, first)
        previous = previous_by_chain.get(key)
        # A conversation keeps its messages as it goes on; Claude Code may rewrite older ones, but
        # their number only grows. Fewer by two or more is another conversation that starts the same
        # way: the same first message after /clear, or a subagent given the prompt an earlier one had.
        # Two subagents that start on one prompt at the same moment still share a chain.
        if previous and len(body.get("messages") or []) < len(previous["messages"]) - 1:
            key = current[first] = f"{first}.{len(chain_numbers)}"
            previous = None
        chain_numbers.setdefault(key, len(chain_numbers) + 1)

        messages = body.get("messages") or []
        tools = body.get("tools") or []
        systems = system_blocks(body)
        answer = reassemble(record)
        row = usage_row(answer["usage"])

        if previous:
            kept = common_prefix(previous["messages"], messages)
            previous_tools = {t.get("name") for t in previous["tools"]}
            current_tools = {t.get("name") for t in tools}
            system_changed = canon(previous["systems"]) != canon(systems)
            tools_added = sorted(current_tools - previous_tools)
            tools_removed = sorted(previous_tools - current_tools)
            new_turn = row["prompt_in"] - previous["row"]["prompt_in"] - previous["row"]["output"]
        else:
            kept = 0
            system_changed = True
            tools_added = sorted(t.get("name", "?") for t in tools)
            tools_removed = []
            new_turn = None

        exchange = {
            "record": record,
            "body": body,
            "chain": chain_numbers[key],
            "chain_key": key,
            "first_in_chain": previous is None,
            "systems": systems,
            "tools": tools,
            "messages": messages,
            "kept": kept,
            "system_changed": system_changed,
            "tools_added": tools_added,
            "tools_removed": tools_removed,
            "breakpoints": breakpoints(body),
            "breakpoints_moved": previous is not None and breakpoints(body) != previous["breakpoints"],
            "answer": answer,
            "row": row,
            "new_turn": new_turn,
        }
        exchanges.append(exchange)
        previous_by_chain[key] = exchange
    return exchanges

# --- small HTML helpers for the page --------------------------------------


def esc(value) -> str:
    return html.escape(str(value), quote=False)
