"""Profile the context of every request in a recording: what the prompt is made of.

Runs offline over the files that the proxy wrote. The page answers "what is one
call made of": a tree of the request on the left, the content of the selected
node on the right, every row sized in characters and in estimated tokens.

The API reports one token count per request and nothing per part. The estimate
spreads that count over the parts in proportion to their size on the wire, so
the rows of a request add up to the real number. The thinking tokens of an
answer are usually invisible in the recording, because Claude Code asks the API
to omit the thinking text; whatever the output count cannot be explained by the
visible answer is put on the thinking node.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from .reader import (
    MESSAGES_PATH,
    block_text,
    build_exchanges,
    esc,
    is_messages_call,
    load_run,
    session_id,
    system_blocks,
)

PAGE_NAME = "context_profiler.html"
FALLBACK_CHARS_PER_TOKEN = 4.0
LABEL_LIMIT = 70


# --- sizes ---------------------------------------------------------------


def wire_size(value) -> int:
    """Characters of a value as the request carries it."""
    return len(json.dumps(value, ensure_ascii=False))


def text_size(text: str) -> int:
    """Characters of a string on the wire, without the quotes around it."""
    return wire_size(text) - 2


# --- labels ----------------------------------------------------------------

TAG_RE = re.compile(r"^\s*<([\w-]+)>\s*(.*?)\s*</\1>\s*$", re.S)

# Pieces the harness puts into the prompt. First match wins. The label is
# best effort: a wrong guess costs nothing, the fallback is the first line.
KNOWN_PIECES = [
    (re.compile(r"Contents of (\S+)"), lambda m: Path(m.group(1)).name),
    (re.compile(r"^x-anthropic-billing-header", re.M), lambda m: "billing header"),
    (re.compile(r"The following skills are available"), lambda m: "skills listing"),
    (re.compile(r"Available agent types"), lambda m: "agent types"),
    (re.compile(r"deferred tools are now available", re.I), lambda m: "deferred tools listing"),
    (re.compile(r"^# MCP Server Instructions", re.M), lambda m: "MCP server instructions"),
    (re.compile(r"You are powered by the model named"), lambda m: "model identity"),
    (re.compile(r"^Today's date", re.M), lambda m: "today's date"),
    (
        re.compile(r"you can use the following context:((?:.*\n)*)"),
        lambda m: "context: " + ", ".join(re.findall(r"^# (\S+)", m.group(1), re.M)),
    ),
    (
        re.compile(r"You are an interactive agent that helps users with software engineering"),
        lambda m: "Claude Code system prompt",
    ),
]


def first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def shorten(text: str, limit: int = LABEL_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def label_text(text: str) -> str:
    """A short name for a block of prompt text."""
    tag = None
    match = TAG_RE.match(text)
    if match:
        tag, text = match.group(1), match.group(2)
    known = None
    for pattern, name in KNOWN_PIECES:
        found = pattern.search(text)
        if found:
            known = name(found)
            break
    if known is None:
        line = first_line(text)
        known = line[2:].strip() if line.startswith("# ") else line
        known = shorten(known)
    return f"{tag}: {known}" if tag else known


# --- sections --------------------------------------------------------------

HEADING_RE = re.compile(r"^(#{1,6}) ")
OPEN_TAG_RE = re.compile(r"^<([\w-]+)>")


def split_sections(text: str) -> list[tuple[str, str]]:
    """Cut a text into its top-level parts: headings of the highest level
    present, and tag blocks. One level deep. Empty when there is nothing to cut."""
    lines = text.splitlines(keepends=True)

    # The heading level to cut on is the highest one used outside code fences.
    level = None
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        found = HEADING_RE.match(line)
        if found and (level is None or len(found.group(1)) < level):
            level = len(found.group(1))

    sections: list[tuple[str, list[str]]] = [("(preamble)", [])]
    in_fence = False
    closing = None  # the line that closes the open tag block
    for line in lines:
        if closing is not None:
            sections[-1][1].append(line)
            if line.strip() == closing:
                closing = None
                sections.append(("", []))
            continue
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence:
            heading = HEADING_RE.match(line)
            if heading and len(heading.group(1)) == level:
                sections.append((line[level + 1 :].strip(), [line]))
                continue
            tag = OPEN_TAG_RE.match(line)
            if tag and not line.rstrip().endswith(f"</{tag.group(1)}>"):
                sections.append((f"<{tag.group(1)}>", [line]))
                closing = f"</{tag.group(1)}>"
                continue
        sections[-1][1].append(line)

    out = []
    for name, part in sections:
        body = "".join(part)
        if not body.strip():
            continue
        if not name:
            name = shorten(first_line(body))
        if name.startswith("<"):
            name = label_text(body)
        out.append((name, body))
    return out if len(out) > 1 else []


# --- listings --------------------------------------------------------------

# Lists of things the harness puts into the prompt, one item each: the user can
# tick an item in the page. (pattern of the header line, label, item kind, form)
# Forms: "bullets" is "- NAME: description" with continuation lines, "names" is
# one bare name per line, "headings" is one "## NAME" section per item.
LISTINGS = [
    (re.compile(r"^The following skills are available.*:\s*$"), "skills", "skill", "bullets"),
    (re.compile(r"^Available agent types.*:\s*$"), "subagent types", "subagent", "bullets"),
    (re.compile(r"^.*deferred tools are now available.*:\s*$", re.I), "deferred tools", "tool", "names"),
    (re.compile(r"^The following tools just became available.*:\s*$"), "added tools", "tool", "names"),
    (re.compile(r"^# MCP Server Instructions\s*$"), "MCP servers", "MCP server", "headings"),
]
BULLET_RE = re.compile(r"^- (\S+?):(?: |$)")
NAME_RE = re.compile(r"^\S+$")
MCP_SERVER_RE = re.compile(r"mcp__([\w-]+?)__")


def mcp_servers(body: dict) -> set[str]:
    """The MCP servers a request names in its tools, as their tool names spell them."""
    return set(MCP_SERVER_RE.findall(json.dumps(body, ensure_ascii=False)))


def server_key(heading: str) -> str:
    """A server's name as Claude Code spells it in tool names: "claude.ai Todoist" is claude_ai_Todoist."""
    return re.sub(r"[^\w-]", "_", heading)


def tool_kind(name: str) -> str:
    return "MCP tool" if name.startswith("mcp__") else "tool"


def ends_listing(line: str) -> bool:
    """A heading of the top level, or the header of the next listing, which can follow without one."""
    return line.startswith("# ") or any(entry[0].match(line) for entry in LISTINGS)


def server_headings(lines: list[str], start: int, servers: set[str]) -> set[str] | None:
    """The "## " headings of a server listing that name a server. A server's instructions can have
    "## " headings of their own; they stay in its text. None when no heading names a known server,
    and every "## " heading starts an item."""
    names = set()
    for line in lines[start + 1 :]:
        if ends_listing(line):
            break
        if line.startswith("## ") and server_key(line[3:].strip()) in servers:
            names.add(line[3:].strip())
    return names or None


def parse_listing(lines: list[str], start: int, form: str, servers: set[str]) -> tuple[list[tuple[str, str]], int]:
    """The items of the listing whose header is lines[start], as (name, text),
    and the index of the line after the listing."""
    items: list[tuple[str, list[str]]] = []
    headings = server_headings(lines, start, servers) if form == "headings" else None
    i = start + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines):
        line = lines[i]
        if form == "names":
            if not NAME_RE.match(line.strip()):
                break
            items.append((line.strip(), [line]))
        elif form == "bullets":
            found = BULLET_RE.match(line)
            if found:
                items.append((found.group(1), [line]))
            elif line.strip() and items and lines[i - 1].strip():
                items[-1][1].append(line)  # a description that goes on for more lines
            elif line.strip():
                break
        else:  # headings; a line of introduction may come before the first
            if ends_listing(line):
                break
            if line.startswith("## ") and (headings is None or line[3:].strip() in headings):
                items.append((line[3:].strip(), [line]))
            elif items:
                items[-1][1].append(line)
        i += 1
    return [(name, "\n".join(body).strip()) for name, body in items], i


def add_listings(item: dict, text: str, path: list[str], store: "Store", servers: set[str]) -> None:
    """Give a text node one child per listing in it, and the listing one child
    per item. Each item carries "pick", which the page offers as a checkbox."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        hit = next((entry for entry in LISTINGS if entry[0].match(lines[i])), None)
        if hit is None:
            i += 1
            continue
        _, label, kind, form = hit
        items, end = parse_listing(lines, i, form, servers)
        if items:
            body = "\n".join(lines[i:end]).strip()
            listing = node(label, "listing", text_size(body), path + [label], store, text=body)
            for name, part in items:
                child = node(name, "item", text_size(part), path + [label, name], store, text=part)
                child["pick"] = {"kind": tool_kind(name) if kind == "tool" else kind, "name": name}
                listing["children"].append(child)
            item["children"].append(listing)
        i = max(end, i + 1)


# --- the tree --------------------------------------------------------------


class Store:
    """Content by hash. Every request replays the previous one, so the same
    text turns up in every tree; the page carries it once."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def put(self, text: str | None) -> str | None:
        if text is None:
            return None
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        self.items.setdefault(key, text)
        return key


def pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def node(label: str, kind: str, chars: int, path: list[str], store: Store, text=None, raw=None, bp=False) -> dict:
    return {
        "label": label,
        "kind": kind,
        "chars": chars,
        "path": "/".join(path),
        "text": store.put(text),
        "raw": store.put(pretty(raw) if raw is not None else None),
        "bp": bp,
        "children": [],
    }


def has_breakpoint(value) -> bool:
    return isinstance(value, dict) and bool(value.get("cache_control"))


def system_tree(blocks: list, store: Store, servers: set[str]) -> dict:
    root = node("system", "group", wire_size(blocks), ["system"], store)
    for i, block in enumerate(blocks):
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        path = ["system", str(i)]
        item = node(label_text(text), "text", wire_size(block), path, store, text=text, raw=block, bp=has_breakpoint(block))
        for name, body in split_sections(text):
            section = node(name, "section", text_size(body), path + [name], store, text=body)
            add_listings(section, body, path + [name], store, servers)
            item["children"].append(section)
        if not item["children"]:
            add_listings(item, text, path, store, servers)
        root["children"].append(item)
    return root


def tools_tree(tools: list, store: Store) -> dict:
    root = node("tools", "group", wire_size(tools), ["tools"], store)
    for i, tool in enumerate(tools):
        name = tool.get("name") or tool.get("type") or f"tool {i}"
        child = node(name, "tool", wire_size(tool), ["tools", name], store, text=pretty(tool), bp=has_breakpoint(tool))
        child["pick"] = {"kind": tool_kind(name), "name": name}
        root["children"].append(child)
    return root


def block_label(block: dict, tool_names: dict[str, str]) -> str:
    kind = block.get("type", "?")
    if kind == "text":
        return label_text(block.get("text", ""))
    if kind == "tool_use":
        tool_names[block.get("id", "")] = block.get("name", "?")
        return f"tool_use {block.get('name', '?')}"
    if kind == "tool_result":
        name = tool_names.get(block.get("tool_use_id", ""), "?")
        return f"tool_result {name}" + (" (error)" if block.get("is_error") else "")
    if kind == "thinking":
        return "thinking" if block.get("thinking") else "thinking (text omitted, signature only)"
    return kind


def message_node(i: int, message: dict, store: Store, tool_names: dict[str, str], servers: set[str]) -> dict:
    role = message.get("role", "?")
    content = message.get("content")
    path = ["messages", str(i)]
    item = node("", role, wire_size(message), path, store, raw=message)
    if isinstance(content, str):
        item["text"] = store.put(content)
        item["label"] = f"{i} {role}: {label_text(content)}"
        item["bp"] = has_breakpoint(message)
        add_listings(item, content, path, store, servers)
        return item
    for j, block in enumerate(content or []):
        label = block_label(block, tool_names)
        kind = block.get("type", "?")
        child = node(label, kind, wire_size(block), path + [str(j)], store, text=block_text(block), raw=block, bp=has_breakpoint(block))
        if kind == "text":
            add_listings(child, block.get("text", ""), path + [str(j)], store, servers)
        item["children"].append(child)
    summary = ", ".join(child["label"] for child in item["children"]) or "(empty)"
    item["label"] = f"{i} {role}: {shorten(summary)}"
    return item


def messages_tree(messages: list, store: Store, servers: set[str]) -> dict:
    root = node("messages", "group", wire_size(messages), ["messages"], store)
    tool_names: dict[str, str] = {}
    for i, message in enumerate(messages):
        root["children"].append(message_node(i, message, store, tool_names, servers))
    return root


def answer_tree(answer: dict, store: Store) -> dict:
    blocks = answer.get("blocks") or []
    root = node("answer", "answer", wire_size(blocks), ["answer"], store)
    tool_names: dict[str, str] = {}
    for j, block in enumerate(blocks):
        kind = block.get("type", "?")
        label = block_label(block, tool_names)
        root["children"].append(node(label, kind, wire_size(block), ["answer", str(j)], store, text=block_text(block), raw=block))
    if answer.get("error"):
        root["children"].append(node("error", "error", 0, ["answer", "error"], store, text=pretty(answer["error"])))
    return root


def assign_segments(request: dict) -> int:
    """Number the cache segments in the order the API hashes the prompt:
    tools, then system, then messages. Returns the number of segments."""
    by_path = {child["path"]: child for child in request["children"]}
    segment = 0

    def walk(item: dict) -> None:
        nonlocal segment
        item["seg"] = segment
        for child in item.get("children", []):
            walk(child)
        if item.get("bp"):
            segment += 1

    for key in ("tools", "system", "messages"):
        if key in by_path:
            walk(by_path[key])
    return segment + 1


def estimate(item: dict, chars_per_token: float) -> None:
    item["tok"] = round(item["chars"] / chars_per_token) if chars_per_token else 0
    for child in item.get("children", []):
        estimate(child, chars_per_token)


def estimate_answer(answer: dict, output_tokens: int, chars_per_token: float) -> None:
    """Visible blocks are estimated like the input. Whatever the API counted on
    top of that is thinking the recording cannot see, and goes on the thinking node."""
    estimate(answer, chars_per_token)
    visible = sum(child["tok"] for child in answer["children"])
    hidden = output_tokens - visible
    thinking = next((c for c in answer["children"] if c["kind"] == "thinking"), None)
    if hidden > 0:
        if thinking is None:
            thinking = node("thinking (not in the recording)", "thinking", 0, ["answer", "thinking"], Store())
            thinking["text"] = None
            thinking["tok"] = 0  # created after estimate() ran, so it has no estimate yet
            answer["children"].insert(0, thinking)
        thinking["tok"] += hidden
        thinking["hidden_tok"] = hidden
    answer["tok"] = output_tokens if output_tokens else visible


def chars_per_token(exchanges: list[dict], position: int) -> tuple[float, str]:
    """The ratio of this request, or of the nearest request in its chain that
    has a token count, or the fallback."""
    own = exchanges[position]
    siblings = [i for i in range(len(exchanges)) if i != position and exchanges[i]["chain_key"] == own["chain_key"]]
    order = [position] + [i for _, i in sorted((abs(i - position), i) for i in siblings)]
    for i in order:
        tokens = exchanges[i]["row"]["prompt_in"]
        if tokens > 0:
            ratio = wire_size(exchanges[i]["body"]) / tokens
            return ratio, "measured" if i == position else f"from request {i + 1}"
    return FALLBACK_CHARS_PER_TOKEN, "fallback"


def request_tree(exchanges: list[dict], position: int, store: Store) -> dict:
    exchange = exchanges[position]
    body = exchange["body"]
    record = exchange["record"]
    # Numbered among the requests, not among all calls: token counts and other calls are left out.
    root = node(f"request {position + 1}", "request", wire_size(body), ["request"], store)
    servers = mcp_servers(body)
    parts = [
        system_tree(system_blocks(body), store, servers),
        tools_tree(body.get("tools") or [], store),
        messages_tree(body.get("messages") or [], store, servers),
    ]
    other = {k: v for k, v in body.items() if k not in ("system", "tools", "messages")}
    envelope = root["chars"] - sum(part["chars"] for part in parts)
    parts.append(node("envelope: " + ", ".join(other), "envelope", envelope, ["envelope"], store, text=pretty(other)))
    root["children"] = parts
    segments = assign_segments(root)

    ratio, ratio_source = chars_per_token(exchanges, position)
    estimate(root, ratio)
    answer = answer_tree(exchange["answer"], store)
    estimate_answer(answer, exchange["row"]["output"], ratio)
    root["children"].append(answer)

    row = exchange["row"]
    root["info"] = {
        "index": record["index"],
        "session": session_id(record),
        "chain": exchange["chain"],
        "first_in_chain": exchange["first_in_chain"],
        "model": body.get("model", "?"),
        "prompt_in": row["prompt_in"],
        "fresh": row["fresh"],
        "cache_read": row["cache_read"],
        "cache_write": row["cache_write"],
        "output": row["output"],
        "duration_ms": record.get("duration_ms", 0),
        "segments": segments,
        "ratio": round(ratio, 2),
        "ratio_source": ratio_source,
        "aborted": bool(record.get("aborted")),
        "status": record.get("status"),
        "stop_reason": exchange["answer"].get("stop_reason"),
    }
    return root


def build_profile(exchanges: list[dict]) -> dict:
    store = Store()
    requests = [request_tree(exchanges, i, store) for i in range(len(exchanges))]
    return {"requests": requests, "content": store.items}


# --- the page --------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e0e0e0;
        --card:#fafafa; --code:#f4f4f5; --accent:#7a3fd4; --bar:#c9b3f2; --answer:#0a7b34;
        --sel:#efe7fb; --warn:#b3261e; --shade:rgba(0,0,0,.35); }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16161a; --fg:#e6e6e6; --muted:#9a9aa5; --line:#2c2c33; --card:#1d1d22;
          --code:#232329; --accent:#b18cf0; --bar:#4b3a73; --answer:#4ac97e; --sel:#2a2438; --warn:#ff7b72; --shade:rgba(0,0,0,.6); }
}
* { box-sizing: border-box; }
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--fg); display:flex; flex-direction:column;
       font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.picker { display:flex; gap:.6rem; align-items:center; padding:.5rem 9rem .5rem 1.5rem; border-bottom:1px solid var(--line); }
.picker .step { font:16px/1 ui-monospace, monospace; padding:.1rem .55rem; }
.tl-label { font:12px ui-monospace, monospace; color:var(--muted); white-space:nowrap; min-width:16rem; text-align:center; }
.timeline { flex:1; min-width:0; height:28px; display:flex; align-items:flex-end; gap:2px; overflow-x:auto; }
.timeline.dense { gap:0; }
.timeline.dense .tl-bar { min-width:1px; border-radius:0; }
.tl-bar { flex:1 1 0; min-width:2px; max-width:14px; background:var(--bar); border-radius:2px 2px 0 0; cursor:pointer; }
.tl-bar:hover { background:var(--muted); }
.tl-bar.on { background:var(--accent); }
.tl-bar.err { background:var(--warn); }
.tl-bar.gap { margin-left:7px; }
button { font:inherit; padding:.3rem .7rem; border:1px solid var(--line); border-radius:6px;
         background:var(--card); color:var(--fg); cursor:pointer; }
button:hover { background:var(--code); }
button:disabled { opacity:.5; cursor:default; }
button.on { background:var(--code); }
.split { flex:1; display:grid; grid-template-columns:minmax(360px, 44%) 1fr; min-height:0; }
.tree, .pane { overflow:auto; min-height:0; }
.tree { border-right:1px solid var(--line); }
.toolbar button { white-space:nowrap; }
.toolbar { display:flex; gap:.5rem; align-items:center; padding:.5rem .8rem; border-bottom:1px solid var(--line);
           position:sticky; top:0; background:var(--bg); z-index:1; font-size:12px; color:var(--muted); }
.row { display:grid; grid-template-columns:1fr 6rem 5rem 3.2rem; align-items:center; gap:.5rem;
       padding:.15rem .8rem .15rem 0; cursor:pointer; border-bottom:1px solid transparent; }
.row:hover { background:var(--code); }
.row.sel { background:var(--sel); }
.row .lbl { display:flex; align-items:center; gap:.3rem; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row .caret { width:1rem; color:var(--muted); flex:none; text-align:center; font-size:11px; }
.row .name { overflow:hidden; text-overflow:ellipsis; }
.row .bar { height:8px; background:var(--code); border-radius:3px; overflow:hidden; }
.row .bar i { display:block; height:100%; background:var(--bar); }
.row.answer .bar i { background:var(--answer); opacity:.5; }
.row .num { font:12px ui-monospace, monospace; text-align:right; color:var(--muted); }
.row .num b { color:var(--fg); font-weight:500; }
.row.answer .name, .row.answer .caret { color:var(--answer); }
.row .bp { font:600 10px/1 ui-monospace, monospace; color:var(--accent); border:1px solid var(--accent);
           border-radius:3px; padding:1px 3px; flex:none; }
.row .seg { font:10px ui-monospace, monospace; color:var(--muted); flex:none; }
.row .tick { margin:0; flex:none; cursor:pointer; }
.pane { padding:1rem 1.5rem; }
.pane h2 { font-size:1rem; margin:0 0 .3rem; word-break:break-word; }
.crumbs { color:var(--muted); font:12px ui-monospace, monospace; margin-bottom:.3rem; }
.crumbs a { color:var(--accent); cursor:pointer; }
.facts { display:flex; flex-wrap:wrap; gap:.3rem .9rem; font-size:13px; color:var(--muted); margin-bottom:.8rem; }
.facts b { color:var(--fg); font-weight:500; font-variant-numeric:tabular-nums; }
.warn { color:var(--warn); }
.ids { display:grid; grid-template-columns:auto 1fr auto; gap:.25rem .6rem; align-items:center; font-size:12px;
       color:var(--muted); margin:-.3rem 0 .9rem; }
.ids code { font:12px ui-monospace, monospace; color:var(--fg); word-break:break-all; user-select:all; }
.ids button { font-size:11px; padding:.05rem .45rem; }
pre { margin:.4rem 0; padding:.6rem .7rem; background:var(--code); border-radius:6px; overflow:auto;
      white-space:pre-wrap; word-break:break-word; font:12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; font-size:13px; margin:.5rem 0 1rem; }
th, td { padding:.3rem .5rem; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
th:first-child, td:first-child { text-align:left; white-space:normal; }
th { color:var(--muted); font-weight:600; cursor:pointer; user-select:none; }
th:hover, th.on { color:var(--fg); }
td a { color:var(--accent); cursor:pointer; }
.note { color:var(--muted); font-size:12px; margin:.5rem 0; }
button.small { font-size:12px; padding:.15rem .5rem; margin-left:auto; }
.jv { font:14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin-top:.6rem; }
.jv-row { display:flex; align-items:baseline; gap:.5rem; padding:.1rem 0; }
.jv-click { cursor:pointer; }
.jv-click:hover .jv-key { color:var(--fg); }
.jv-caret { width:.8rem; color:var(--muted); font-size:11px; flex:none; }
.jv-key { color:var(--muted); font:12px ui-monospace, monospace; }
.jv-val { font:13px ui-monospace, monospace; color:var(--accent); word-break:break-word; }
.jv-sum { color:var(--muted); font-size:12px; }
.jv-tok { margin-left:auto; color:var(--muted); font:11px ui-monospace, monospace; flex:none; }
.jv-kids { margin-left:.35rem; padding-left:.9rem; border-left:1px solid var(--line); }
.jv-text { max-width:80ch; margin:.2rem 0 .6rem; }
.jv-text p { margin:.45rem 0; white-space:pre-wrap; word-break:break-word; }
.jv-text ul { margin:.3rem 0; padding-left:1.2rem; }
.jv-text li { margin:.15rem 0; white-space:pre-wrap; word-break:break-word; }
.jv-text .jv-num { color:var(--muted); }
.jv-text .jv-h { font-weight:600; margin:.8rem 0 .2rem; }
.jv-text code { font:12px ui-monospace, monospace; background:var(--code); padding:0 .25rem; border-radius:3px; }
.jv-tag { display:inline-block; font:11px ui-monospace, monospace; color:var(--muted); border:1px solid var(--line);
          border-radius:3px; padding:0 .3rem; margin:.3rem 0; }
dialog { width:min(720px, 92vw); border:1px solid var(--line); border-radius:10px; padding:1rem 1.2rem;
         background:var(--bg); color:var(--fg); }
dialog::backdrop { background:var(--shade); }
dialog h2 { font-size:1rem; margin:0; }
dialog pre { max-height:55vh; }
dialog .actions { display:flex; gap:.5rem; justify-content:flex-end; margin-top:.8rem; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
"""

JS = r"""
(function () {
    var data = JSON.parse(document.getElementById('data').textContent);
    var requests = data.requests, content = data.content;
    // paneSort is null while the table follows the tree's sort toggle; clicking
    // a column header gives the table an order of its own.
    var state = { req: 0, path: 'request', sortBySize: true, open: {}, jsonOpen: {}, rawView: false, paneSort: null, picked: {}, dialog: false };
    var $ = function (id) { return document.getElementById(id); };
    var n = function (x) { return (x || 0).toLocaleString('en-US'); };
    var pct = function (a, b) { return b ? (100 * a / b).toFixed(1) + '%' : '-'; };
    var el = function (tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    };

    function root() { return requests[state.req]; }
    function answerOf(r) { return r.children[r.children.length - 1]; }
    function base(item) { return item.path.indexOf('answer') === 0 ? answerOf(root()) : root(); }
    function find(item, path) {
        if (item.path === path) return item;
        for (var i = 0; i < item.children.length; i++) {
            var hit = find(item.children[i], path);
            if (hit) return hit;
        }
        return null;
    }
    function parentOf(item, path) {
        for (var i = 0; i < item.children.length; i++) {
            if (item.children[i].path === path) return item;
            var hit = parentOf(item.children[i], path);
            if (hit) return hit;
        }
        return null;
    }
    // Keep the selection on the same node when switching request, or on the
    // nearest ancestor that exists there.
    function resolve(path) {
        var parts = path.split('/');
        while (parts.length) {
            if (find(root(), parts.join('/'))) return parts.join('/');
            parts.pop();
        }
        return 'request';
    }
    function isOpen(item, depth) {
        var o = state.open[item.path];
        return o === undefined ? depth < 1 : o;
    }

    // One bar per request, its height the prompt's tokens, so a long session stays one row and shows
    // how the context grew. A gap starts a new conversation chain; a failed call is red.
    function failed(info) { return info.status >= 400 || info.aborted; }
    function renderPicker() {
        var box = $('picker');
        box.innerHTML = '';
        var info = root().info;
        var prev = el('button', 'step', '\u2039'), next = el('button', 'step', '\u203a');
        prev.title = 'previous request (\u2190)'; next.title = 'next request (\u2192)';
        prev.disabled = state.req === 0; next.disabled = state.req === requests.length - 1;
        prev.onclick = function () { select(state.req - 1, state.path); };
        next.onclick = function () { select(state.req + 1, state.path); };
        box.appendChild(prev);
        box.appendChild(el('span', 'tl-label', 'request ' + (state.req + 1) + ' of ' + requests.length + ' \u00b7 ' +
            (failed(info) ? (info.status >= 400 ? 'HTTP ' + info.status : 'aborted') : n(info.prompt_in) + ' tokens')));
        box.appendChild(next);
        var strip = el('div', 'timeline' + (requests.length > 120 ? ' dense' : ''));
        var top = Math.max.apply(null, requests.map(function (r) { return r.info.prompt_in; }).concat([1]));
        requests.forEach(function (r, i) {
            var bar = el('div', 'tl-bar' + (i === state.req ? ' on' : '') + (failed(r.info) ? ' err' : '') +
                         (i && r.info.chain !== requests[i - 1].info.chain ? ' gap' : ''));
            bar.style.height = Math.max(10, 100 * r.info.prompt_in / top) + '%';
            bar.title = 'request ' + (i + 1) + ' \u00b7 ' + n(r.info.prompt_in) + ' tokens \u00b7 ' + r.info.model +
                        ' \u00b7 chain ' + r.info.chain + (failed(r.info) ? ' \u00b7 failed' : '');
            bar.onclick = function () { select(i, state.path); };
            strip.appendChild(bar);
        });
        box.appendChild(strip);
        // In a narrow window the strip scrolls; keep the selected bar in view.
        var on = strip.querySelector('.tl-bar.on');
        if (on && strip.scrollWidth > strip.clientWidth) {
            strip.scrollLeft = on.offsetLeft - strip.offsetLeft - strip.clientWidth / 2;
        }
    }

    function renderTree() {
        var box = $('rows');
        box.innerHTML = '';
        var r = root();
        var walk = function (item, depth, total, answer, parentSeg) {
            var row = el('div', 'row' + (item.path === state.path ? ' sel' : '') + (answer ? ' answer' : ''));
            row.style.paddingLeft = (0.4 + depth * 1.1) + 'rem';
            var lbl = el('div', 'lbl');
            var caret = el('span', 'caret', item.children.length ? (isOpen(item, depth) ? '▾' : '▸') : '');
            caret.onclick = function (ev) { ev.stopPropagation(); state.open[item.path] = !isOpen(item, depth); renderTree(); };
            lbl.appendChild(caret);
            if (item.pick) {
                var tick = el('input', 'tick');
                tick.type = 'checkbox';
                tick.checked = !!state.picked[pickKey(item.pick)];
                tick.title = 'select ' + item.pick.name;
                tick.onclick = function (ev) { ev.stopPropagation(); };
                tick.onchange = function () { togglePick(item.pick, tick.checked); };
                lbl.appendChild(tick);
            }
            lbl.appendChild(el('span', 'name', item.label));
            if (item.bp) lbl.appendChild(el('span', 'bp', 'cache'));
            if (item.seg !== undefined && item.seg !== parentSeg) lbl.appendChild(el('span', 'seg', 'seg ' + (item.seg + 1)));
            row.appendChild(lbl);
            var bar = el('div', 'bar'); var fill = el('i');
            fill.style.width = (total ? Math.max(0.5, 100 * item.tok / total) : 0) + '%';
            bar.appendChild(fill); row.appendChild(bar);
            var num = el('div', 'num'); num.innerHTML = '<b>' + n(item.tok) + '</b>';
            row.appendChild(num);
            row.appendChild(el('div', 'num', pct(item.tok, total)));
            row.onclick = function () { select(state.req, item.path); };
            box.appendChild(row);
            if (!item.children.length || !isOpen(item, depth)) return;
            var kids = item.children.slice();
            if (state.sortBySize) kids.sort(function (a, b) { return b.tok - a.tok; });
            kids.forEach(function (k) {
                var isAnswer = answer || k.kind === 'answer';
                walk(k, depth + 1, isAnswer ? answerOf(r).tok : total, isAnswer, item.seg);
            });
        };
        walk(r, 0, r.tok, false, undefined);
    }

    function crumbs(item) {
        var box = el('div', 'crumbs');
        var parts = item.path === 'request' ? [] : item.path.split('/');
        var paths = ['request'];
        for (var i = 0; i < parts.length; i++) paths.push(parts.slice(0, i + 1).join('/'));
        paths.forEach(function (p, i) {
            var hit = find(root(), p);
            var a = el('a', '', hit ? hit.label : p);
            a.onclick = function () { select(state.req, p); };
            if (i) box.appendChild(document.createTextNode(' › '));
            box.appendChild(a);
        });
        return box;
    }

    function fact(box, label, value, cls) {
        var s = el('span', cls || '');
        s.appendChild(document.createTextNode(label + ' '));
        s.appendChild(el('b', '', value));
        box.appendChild(s);
    }

    // The session ID and the recording's folder, to paste into a terminal or a bug report.
    function ids(info) {
        var box = el('div', 'ids');
        [['session', info.session], ['folder', data.run_dir]].forEach(function (pair) {
            if (!pair[1]) return;
            var b = el('button', '', 'copy');
            b.onclick = function () {
                copyText(pair[1], function () { b.textContent = 'copied'; }, function () { b.textContent = 'select it'; });
            };
            box.appendChild(el('span', '', pair[0]));
            box.appendChild(el('code', '', pair[1]));
            box.appendChild(b);
        });
        return box;
    }

    function renderPane() {
        var pane = $('pane');
        pane.innerHTML = '';
        var r = root(), info = r.info, item = find(r, state.path) || r;
        var total = base(item).tok, inAnswer = item.path.indexOf('answer') === 0;
        pane.appendChild(crumbs(item));
        pane.appendChild(el('h2', '', item.label));
        var facts = el('div', 'facts');
        if (item.kind === 'request') {
            fact(facts, 'model', info.model);
            fact(facts, 'prompt', n(info.prompt_in) + ' tokens');
            fact(facts, 'fresh', n(info.fresh));
            fact(facts, 'cache read', n(info.cache_read));
            fact(facts, 'cache write', n(info.cache_write));
            fact(facts, 'output', n(info.output) + ' tokens');
            fact(facts, 'cache segments', info.segments);
            fact(facts, 'took', n(info.duration_ms) + ' ms');
            if (info.stop_reason) fact(facts, 'stop', info.stop_reason);
            if (info.aborted) fact(facts, '', 'aborted', 'warn');
            if (info.status >= 400) fact(facts, 'HTTP', info.status, 'warn');
        } else {
            fact(facts, '≈ tokens', n(item.tok));
            fact(facts, inAnswer ? 'of output' : 'of prompt', pct(item.tok, total));
            if (item.hidden_tok) fact(facts, 'not visible', n(item.hidden_tok) + ' tokens');
            if (item.seg !== undefined) fact(facts, 'cache segment', (item.seg + 1) + ' of ' + info.segments);
            if (item.bp) fact(facts, '', 'breakpoint ends here', 'warn');
        }
        pane.appendChild(facts);
        if (item.kind === 'request') pane.appendChild(ids(info));

        if (item.children.length) {
            var t = el('table');
            var head = el('tr');
            [['part', 'wire'], ['≈ tokens', 'size'], ['share', 'size']].forEach(function (col) {
                var sort = state.paneSort || { key: state.sortBySize ? 'size' : 'wire', desc: state.sortBySize };
                var active = sort.key === col[1] && (col[1] !== 'size' || col[0] === '≈ tokens');
                var th = el('th', active ? 'on' : '', col[0] + (active ? (sort.desc ? ' ▾' : ' ▴') : ''));
                th.title = 'sort by ' + col[0];
                th.onclick = function () {
                    var same = sort.key === col[1];
                    state.paneSort = { key: col[1], desc: same ? !sort.desc : col[1] === 'size' };
                    renderPane();
                };
                head.appendChild(th);
            });
            t.appendChild(head);
            var kids = item.children.slice();
            var order = state.paneSort || { key: state.sortBySize ? 'size' : 'wire', desc: state.sortBySize };
            if (order.key === 'size') kids.sort(function (a, b) { return b.tok - a.tok; });
            if (order.desc !== (order.key === 'size')) kids.reverse();
            kids.forEach(function (k) {
                var tr = el('tr');
                var td = el('td'); var a = el('a', '', k.label);
                a.onclick = function () { select(state.req, k.path); };
                td.appendChild(a); tr.appendChild(td);
                tr.appendChild(el('td', '', n(k.tok)));
                tr.appendChild(el('td', '', pct(k.tok, k.kind === 'answer' ? k.tok : (item.kind === 'answer' ? item.tok : total))));
                t.appendChild(tr);
            });
            pane.appendChild(t);
        }
        // Structured parts show their JSON as a readable tree; plain text parts
        // (system prompt sections, listed items) show the text alone.
        var source = item.raw ? content[item.raw]
                   : (item.kind === 'tool' || item.kind === 'envelope') && item.text ? content[item.text] : null;
        var plain = !source && item.text ? content[item.text] : null;
        if (source || plain) {
            var view = el('button', 'small', state.rawView ? 'View formatted' : source ? 'View JSON' : 'View raw');
            view.onclick = function () { state.rawView = !state.rawView; renderPane(); persist(); };
            facts.appendChild(view);
            if (state.rawView) pane.appendChild(el('pre', '', source || plain));
            else pane.appendChild(source ? jsonView(JSON.parse(source), item.path) : readableText(plain));
        } else if (!item.children.length) {
            pane.appendChild(el('p', 'note', 'No content in the recording.'));
        }
    }

    // --- readable raw ------------------------------------------------------
    // The exact JSON of a part, every key in its order, rendered to read:
    // dim keys, long strings as formatted text, a token estimate per object
    // and long string. Recorded text is untrusted, so every node is built with
    // textContent.
    function jsonView(value, base) {
        var ratio = root().info.ratio || 4;
        // Characters as Python's json.dumps writes them, with a space after
        // ':' and ',', so the numbers match the rest of the page.
        var wire = function (v) {
            if (v === null || typeof v !== 'object') return JSON.stringify(v).length;
            var list = Array.isArray(v), keys = list ? v.map(function (x, i) { return i; }) : Object.keys(v);
            var size = 2 + Math.max(0, keys.length - 1) * 2;
            keys.forEach(function (k) { size += wire(v[k]) + (list ? 0 : JSON.stringify(k).length + 2); });
            return size;
        };
        var tokens = function (v) { return Math.round(wire(v) / ratio); };
        var isOpen = function (path, depth, v) {
            var o = state.jsonOpen[path];
            return o === undefined ? depth === 0 || tokens(v) <= 1500 : o;
        };
        var badge = function (row, v) {
            var t = tokens(v);
            if (t >= 20) row.appendChild(el('span', 'jv-tok', '≈ ' + n(t)));
        };
        var build = function (key, v, path, depth) {
            var wrap = el('div', 'jv-node'), row = el('div', 'jv-row');
            if (v !== null && typeof v === 'object') {
                var list = Array.isArray(v), keys = list ? v.map(function (x, i) { return i; }) : Object.keys(v);
                var open = isOpen(path, depth, v);
                row.className += ' jv-click';
                row.appendChild(el('span', 'jv-caret', open ? '▾' : '▸'));
                if (key !== null) row.appendChild(el('span', 'jv-key', key));
                if (!open) row.appendChild(el('span', 'jv-sum', list ? keys.length + ' items' : keys.length + ' keys'));
                badge(row, v);
                row.onclick = function () { state.jsonOpen[path] = !open; renderPane(); };
                wrap.appendChild(row);
                if (open) {
                    var kids = el('div', 'jv-kids');
                    keys.forEach(function (k) { kids.appendChild(build(String(k), v[k], path + '/' + k, depth + 1)); });
                    wrap.appendChild(kids);
                }
            } else if (typeof v === 'string' && (v.length > 80 || v.indexOf('\n') >= 0)) {
                if (key !== null) row.appendChild(el('span', 'jv-key', key));
                badge(row, v);
                wrap.appendChild(row);
                wrap.appendChild(readableText(v));
            } else {
                if (key !== null) row.appendChild(el('span', 'jv-key', key));
                row.appendChild(el('span', 'jv-val', typeof v === 'string' ? '"' + v + '"' : String(v)));
                wrap.appendChild(row);
            }
            return wrap;
        };
        var box = el('div', 'jv');
        if (value !== null && typeof value === 'object') {
            (Array.isArray(value) ? value.map(function (x, i) { return i; }) : Object.keys(value)).forEach(function (k) {
                box.appendChild(build(String(k), value[k], base + '/' + k, 1));
            });
        } else {
            box.appendChild(build(null, value, base, 0));
        }
        return box;
    }

    // Light Markdown: headings, lists, code fences, `code` and **bold**. Lines
    // that are only a tag, like <system-reminder>, become small labels.
    function readableText(text) {
        var box = el('div', 'jv-text'), lines = text.split('\n'), para = null, list = null;
        var inline = function (target, s) {
            s.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/).forEach(function (part) {
                if (!part) return;
                if (part.charAt(0) === '`' && part.length > 2) target.appendChild(el('code', '', part.slice(1, -1)));
                else if (part.slice(0, 2) === '**' && part.length > 4) target.appendChild(el('strong', '', part.slice(2, -2)));
                else target.appendChild(document.createTextNode(part));
            });
        };
        var end = function () { para = null; list = null; };
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i], m;
            if (/^\s*```/.test(line)) {
                var code = [];
                for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
                end(); box.appendChild(el('pre', '', code.join('\n')));
            } else if (/^\s*<\/?[A-Za-z][\w-]*>\s*$/.test(line)) {
                end(); box.appendChild(el('div', 'jv-tag', line.trim()));
            } else if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
                end(); var h = el('div', 'jv-h'); inline(h, m[2]); box.appendChild(h);
            } else if ((m = /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(line))) {
                para = null;
                if (!list) { list = el('ul'); box.appendChild(list); }
                var li = el('li'); li.style.marginLeft = (m[1].length / 2) + 'em';
                if (/\d/.test(m[2])) li.appendChild(el('span', 'jv-num', m[2] + ' '));
                inline(li, m[3]); list.appendChild(li);
            } else if (!line.trim()) {
                end();
            } else {
                list = null;
                if (!para) { para = el('p'); box.appendChild(para); } else para.appendChild(document.createTextNode('\n'));
                inline(para, line);
            }
        }
        return box;
    }

    // --- selected items ---------------------------------------------------
    // A tool, skill, subagent type or MCP server can be ticked wherever it shows
    // up; the same kind and name is one selection across all requests.
    function pickKey(pick) { return pick.kind + ': ' + pick.name; }
    function picked() { return Object.keys(state.picked).sort(); }
    function togglePick(pick, on) {
        if (on) state.picked[pickKey(pick)] = true; else delete state.picked[pickKey(pick)];
        renderTree(); renderPickBar(); persist();
    }
    function renderPickBar() {
        var count = picked().length;
        $('copy').disabled = !count;
        $('copy').textContent = count ? 'Disable prompt (' + count + ')' : 'Disable prompt';
        $('clear').disabled = !count;
    }
    // The prompt that asks Claude to switch the selected items off. Tools and
    // subagent types map to one exact deny entry each; for skills and MCP
    // servers the right entry depends on where they come from, so the prompt
    // gives Claude the rule and has it check with the user.
    function disablePrompt() {
        var deny = [], skills = [], servers = [];
        picked().forEach(function (key) {
            var cut = key.indexOf(': '), kind = key.slice(0, cut), name = key.slice(cut + 2);
            if (kind === 'tool' || kind === 'MCP tool') deny.push(name);
            else if (kind === 'subagent') deny.push('Agent(' + name + ')');
            else if (kind === 'skill') skills.push(name);
            else if (kind === 'MCP server') servers.push(name);
        });
        var count = picked().length;
        var L = ['Switch off ' + (count === 1 ? 'this part' : 'these ' + count + ' parts') +
                 ' of my Claude Code context by editing ~/.claude/settings.json.', ''];
        if (deny.length) {
            L.push('Add these entries to "permissions" > "deny", and keep the entries already there:');
            deny.forEach(function (entry) { L.push('- ' + JSON.stringify(entry)); });
            L.push('');
        }
        if (skills.length) {
            L.push('Switch off these skills: ' + skills.join(', ') + '.');
            L.push('For a skill of my own, set "skillOverrides": {"NAME": "off"}. A plugin\'s skill goes only with ' +
                   'its whole plugin: tell me which plugin it belongs to, and ask me before you set ' +
                   '"NAME@MARKETPLACE": false in "enabledPlugins". If you are not sure an entry switches a skill ' +
                   'off, tell me instead of guessing.');
            L.push('');
        }
        if (servers.length) {
            L.push('Switch off these MCP servers: ' + servers.join(', ') + '.');
            L.push('Add "mcp__SERVER" to "permissions" > "deny", with SERVER written exactly as it appears in the ' +
                   'server\'s tool names (mcp__SERVER__TOOL).');
            L.push('');
        }
        L.push('Before you write, show me the exact change and ask me. Leave everything else in the file as it is.');
        L.push('Then tell me that the context is smaller from my next session on, and that I can bring an item ' +
               'back by deleting its entry.');
        return L.join('\n') + '\n';
    }
    function copyText(text, done, fail) {
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(done, function () { copyByHand(text) ? done() : fail(); });
        } else {
            copyByHand(text) ? done() : fail();
        }
    }
    // For a page opened from disk, where the clipboard API may be missing.
    function copyByHand(text) {
        var area = el('textarea');
        area.value = text;
        area.style.position = 'fixed'; area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(area);
        return ok;
    }

    // The dialog shows the prompt before it is copied: Claude Code folds a pasted
    // block into "[Pasted text]", so the page is the only place to read it.
    function openPrompt() {
        var count = picked().length;
        $('prompt-title').textContent = 'Disable prompt (' + count + (count === 1 ? ' item)' : ' items)');
        $('prompt-text').textContent = disablePrompt();
        $('prompt-copy').textContent = 'Copy';
        if (!$('prompt-dialog').open) $('prompt-dialog').showModal();
        state.dialog = true; persist();
    }

    function select(req, path) {
        state.req = req;
        state.path = resolve(path);
        var parts = state.path.split('/');
        for (var i = 1; i < parts.length; i++) state.open[parts.slice(0, i).join('/')] = true;
        renderPicker(); renderTree(); renderPane();
        persist();
    }

    // The live viewer reloads the page when a call lands. Keep the selection
    // across that reload, and stay on the newest request if that is where the
    // reader was.
    var storeKey = 'profile:' + document.title;
    function persist() {
        try {
            sessionStorage.setItem(storeKey, JSON.stringify({
                req: state.req, atEnd: state.req === requests.length - 1, path: state.path,
                sortBySize: state.sortBySize, open: state.open, picked: state.picked, dialog: state.dialog, rawView: state.rawView
            }));
        } catch (e) { /* no storage: the page still works, it just forgets */ }
    }
    function restore() {
        try { return JSON.parse(sessionStorage.getItem(storeKey)); } catch (e) { return null; }
    }

    $('sort').onclick = function () { state.sortBySize = !state.sortBySize; state.paneSort = null; $('sort').className = state.sortBySize ? 'on' : ''; renderTree(); renderPane(); persist(); };
    $('expand').onclick = function () { var all = function (it) { state.open[it.path] = true; it.children.forEach(all); }; all(root()); renderTree(); persist(); };
    $('collapse').onclick = function () { state.open = {}; renderTree(); persist(); };
    $('copy').onclick = openPrompt;
    $('prompt-copy').onclick = function () {
        var b = $('prompt-copy');
        copyText($('prompt-text').textContent,
                 function () { b.textContent = 'Copied'; },
                 function () { b.textContent = 'Copy failed: select the text'; });
    };
    $('prompt-close').onclick = function () { $('prompt-dialog').close(); };
    $('prompt-dialog').addEventListener('close', function () { state.dialog = false; persist(); });
    $('clear').onclick = function () { state.picked = {}; renderTree(); renderPickBar(); persist(); };
    document.addEventListener('keydown', function (ev) {
        if ($('prompt-dialog').open) return;
        if (ev.key === 'ArrowRight' && state.req < requests.length - 1) select(state.req + 1, state.path);
        if (ev.key === 'ArrowLeft' && state.req > 0) select(state.req - 1, state.path);
    });
    var saved = restore();
    if (saved) {
        if (saved.sortBySize !== undefined) state.sortBySize = !!saved.sortBySize;
        state.open = saved.open || {};
        state.picked = saved.picked || {};
        state.rawView = !!saved.rawView;
        select(saved.atEnd ? requests.length - 1 : Math.min(saved.req || 0, requests.length - 1), saved.path || 'request');
        // A live reload while the dialog is open brings it back.
        if (saved.dialog && picked().length) openPrompt();
    } else {
        select(0, 'request');
    }
    $('sort').className = state.sortBySize ? 'on' : '';
    renderPickBar();
})();
"""


def render_page(run_dir: Path, profile: dict) -> str:
    payload = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>context profile {esc(run_dir.name)}</title>
<style>{CSS}</style></head>
<body>
<div class="picker" id="picker" title="&larr; &rarr; switch request"></div>
<div class="split">
  <div class="tree">
    <div class="toolbar">
      <button id="sort">sort by size</button>
      <button id="expand">expand all</button>
      <button id="collapse">collapse</button>
      <button id="copy" title="show a prompt that asks Claude to switch the selected items off">Disable prompt</button>
      <button id="clear">clear</button>
    </div>
    <div id="rows"></div>
  </div>
  <div class="pane" id="pane"></div>
</div>
<dialog id="prompt-dialog">
  <h2 id="prompt-title"></h2>
  <p class="note">Paste it into Claude: it shows the change to your settings and asks before it writes.</p>
  <pre id="prompt-text"></pre>
  <div class="actions"><button id="prompt-copy" class="primary">Copy</button><button id="prompt-close">Close</button></div>
</dialog>
<script id="data" type="application/json">{payload}</script>
<script>{JS}</script>
</body></html>
"""


def render_run(run_dir: Path) -> str:
    """The page for the current state of a run. Raises ValueError when the run
    holds no messages call yet, because the page has nothing to show then."""
    records = load_run(run_dir)
    calls = [r for r in records if is_messages_call(r)]
    if not calls:
        raise ValueError(f"{len(records)} records in {run_dir}, none of them a {MESSAGES_PATH} call")
    exchanges = build_exchanges(calls)
    run_dir = run_dir.resolve()
    profile = build_profile(exchanges)
    profile["run_dir"] = str(run_dir)
    return render_page(run_dir, profile)


def build(run_dir: Path, out: Path | None = None) -> Path:
    """Write the page, by default to <run>/context_profiler.html, and return its path."""
    run_dir = Path(run_dir)
    out = Path(out) if out else run_dir / PAGE_NAME
    out.write_text(render_run(run_dir), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="run directory")
    parser.add_argument("-o", "--out", help=f"output file. Default <run>/{PAGE_NAME}")
    args = parser.parse_args()

    run_dir = Path(args.run)
    if not run_dir.is_dir():
        print(f"profiler: no such run directory: {run_dir}", file=sys.stderr)
        return 1
    try:
        out = build(run_dir, Path(args.out) if args.out else None)
    except (OSError, ValueError) as error:
        print(f"profiler: {error}", file=sys.stderr)
        return 1
    print(f"profiler: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
