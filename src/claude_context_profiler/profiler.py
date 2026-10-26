"""Profile the context of every request in a recording: what the prompt is made of.

Runs offline over the files that the proxy wrote. The page answers "what is one
call made of": a tree of the request on the left, the content of the selected
node on the right, every row sized in estimated tokens.

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
from datetime import datetime
from pathlib import Path

from .reader import (
    MESSAGES_PATH,
    block_text,
    build_exchanges,
    canon,
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
    (re.compile(r"^.*deferred tools are now available.*:\s*$", re.I), "deferred tools (names only)", "tool", "names"),
    (re.compile(r"^The following tools just became available.*:\s*$"), "added tools", "tool", "names"),
    (re.compile(r"^# MCP Server Instructions\s*$"), "MCP servers", "MCP server", "headings"),
]
BULLET_RE = re.compile(r"^- (\S+?):(?: |$)")
NAME_RE = re.compile(r"^\S+$")
MCP_SERVER_RE = re.compile(r"mcp__([\w-]+?)__")


def mcp_servers(exchange: dict) -> set[str]:
    """The MCP servers a request names in its tools, as their tool names spell them. Kept on the
    exchange, since the whole request is written out to find them."""
    if "mcp_servers" not in exchange:
        exchange["mcp_servers"] = set(MCP_SERVER_RE.findall(json.dumps(exchange["body"], ensure_ascii=False)))
    return exchange["mcp_servers"]


def server_key(heading: str) -> str:
    """A server's name as Claude Code spells it in tool names: "claude.ai Todoist" is claude_ai_Todoist."""
    return re.sub(r"[^\w-]", "_", heading)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (dict, list)):
        for item in value.values() if isinstance(value, dict) else value:
            yield from strings(item)


def server_names(exchanges: list[dict]) -> dict[str, str]:
    """Each MCP server's name by its key, from the "## NAME" headings of the server instructions, which
    name it as deniedMcpServers does. A server that sends no instructions is known by its key only."""
    names = {}
    for exchange in exchanges:
        keys = mcp_servers(exchange)
        for text in strings(exchange["body"]):
            if "# MCP Server Instructions" in text:
                for heading in re.findall(r"^## (.+)$", text, re.M):
                    if server_key(heading.strip()) in keys:
                        names[server_key(heading.strip())] = heading.strip()
    return names


def tool_kind(name: str) -> str:
    return "MCP tool" if name.startswith("mcp__") else "tool"


def ends_listing(line: str) -> bool:
    """A heading of the top level, or the header of the next listing, which can follow without one."""
    return line.startswith("# ") or any(entry[0].match(line) for entry in LISTINGS)


def server_headings(lines: list[str], start: int, servers: dict[str, str]) -> set[str] | None:
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


def parse_listing(lines: list[str], start: int, form: str, servers: dict[str, str]) -> tuple[list[tuple[str, str]], int]:
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


def add_listings(item: dict, text: str, path: list[str], store: "Store", servers: dict[str, str]) -> None:
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
                if tool_kind(name) != "MCP tool":
                    child["pick"] = {"kind": "tool" if kind == "tool" else kind, "name": name}
                listing["children"].append(child)
            if kind == "tool":
                group_by_server(listing, servers, store)
            item["children"].append(listing)
        i = max(end, i + 1)
    # A text with several listings is named after all of them, largest first, not after the first
    # one it was recognized by, so that none hides behind another's name.
    listings = [child for child in item["children"] if child["kind"] == "listing"]
    if len(listings) > 1:
        names = [child["label"].split(" (")[0] for child in sorted(listings, key=lambda c: -c["chars"])]
        item["label"] = shorten("listings: " + ", ".join(names))


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


def system_tree(blocks: list, store: Store, servers: dict[str, str]) -> dict:
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


def tools_tree(tools: list, store: Store, servers: dict[str, str]) -> dict:
    root = node("tools", "group", wire_size(tools), ["tools"], store)
    for i, tool in enumerate(tools):
        name = tool.get("name") or tool.get("type") or f"tool {i}"
        child = node(name, "tool", wire_size(tool), ["tools", name], store, text=pretty(tool), bp=has_breakpoint(tool))
        if tool_kind(name) != "MCP tool":
            child["pick"] = {"kind": "tool", "name": name}
        root["children"].append(child)
    group_by_server(root, servers, store)
    return root


def group_by_server(parent: dict, servers: dict[str, str], store: Store) -> None:
    """Gather a node's MCP tools under one node per server, which carries the server's checkbox. A
    server is switched off as a whole: one of its tools costs a few tokens as a deferred name, and
    denying it would leave the server connected."""
    groups: dict[str, dict] = {}
    children = []
    for child in parent["children"]:
        found = MCP_SERVER_RE.match(child["label"])
        if not found:
            children.append(child)
            continue
        key = found.group(1)
        if key not in groups:
            name = servers.get(key, key)
            groups[key] = node(name, "server", 0, parent["path"].split("/") + [name], store)
            groups[key]["pick"] = {"kind": "MCP server", "name": name}
            children.append(groups[key])
        group = groups[key]
        child["path"] = group["path"] + "/" + child["label"]
        group["children"].append(child)
        group["chars"] += child["chars"]
    for group in groups.values():
        count = len(group["children"])
        group["label"] += f" ({count} tool{'' if count == 1 else 's'})"
    parent["children"] = children


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


AGENT_TOOLS = ("Agent", "Task")


def agent_call(block: dict, tool_names: dict[str, str]) -> str | None:
    """The call id of a block that starts a subagent or carries its result, so the page can link it
    to the subagent's requests."""
    if block.get("type") == "tool_use" and block.get("name") in AGENT_TOOLS:
        return block.get("id")
    if block.get("type") == "tool_result" and tool_names.get(block.get("tool_use_id", "")) in AGENT_TOOLS:
        return block.get("tool_use_id")
    return None


def message_node(i: int, message: dict, store: Store, tool_names: dict[str, str], servers: dict[str, str]) -> dict:
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
        call = agent_call(block, tool_names)
        if call:
            child["call"] = call
        if kind == "text":
            add_listings(child, block.get("text", ""), path + [str(j)], store, servers)
        item["children"].append(child)
    summary = ", ".join(child["label"] for child in item["children"]) or "(empty)"
    item["label"] = f"{i} {role}: {shorten(summary)}"
    return item


def messages_tree(messages: list, store: Store, servers: dict[str, str], previous: list) -> dict:
    """The messages, reusing the node of each message the previous request in the chain had too:
    every request replays the conversation, and building it again each time is quadratic."""
    children = []
    tool_names: dict[str, str] = {}
    for i, message in enumerate(messages):
        if i < len(previous) and previous[i][0] == message:
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                block_label(block, tool_names)  # a later tool_result is named after this tool_use
            children.append(previous[i][1])
        else:
            children.append(message_node(i, message, store, tool_names, servers))
    # The size json.dumps would give the list, without writing it out again.
    size = 2 + sum(child["chars"] for child in children) + 2 * max(len(children) - 1, 0)
    root = node("messages", "group", size, ["messages"], store)
    root["children"] = children
    return root


def answer_tree(answer: dict, store: Store) -> dict:
    blocks = answer.get("blocks") or []
    root = node("answer", "answer", wire_size(blocks), ["answer"], store)
    tool_names: dict[str, str] = {}
    for j, block in enumerate(blocks):
        kind = block.get("type", "?")
        label = block_label(block, tool_names)
        child = node(label, kind, wire_size(block), ["answer", str(j)], store, text=block_text(block), raw=block)
        call = agent_call(block, tool_names)
        if call:
            child["call"] = call
        root["children"].append(child)
    if answer.get("error"):
        root["children"].append(node("error", "error", 0, ["answer", "error"], store, text=pretty(answer["error"])))
    return root


def segments(request: dict) -> tuple[dict[str, int], int]:
    """The cache segment of each part, by path, in the order the API hashes the prompt: tools, then
    system, then messages; and the number of segments. The page works them out the same way."""
    by_path = {child["path"]: child for child in request["children"]}
    seg: dict[str, int] = {}
    segment = 0

    def walk(item: dict) -> None:
        nonlocal segment
        seg[item["path"]] = segment
        for child in item.get("children", []):
            walk(child)
        if item.get("bp"):
            segment += 1

    for key in ("tools", "system", "messages"):
        if key in by_path:
            walk(by_path[key])
    return seg, segment + 1


def tokens(item: dict, chars_per_token: float) -> int:
    """A part's estimated tokens. Parts are shared between requests, which each have their own ratio,
    so the estimate is worked out when needed; only the answer's parts carry theirs ("tok")."""
    if "tok" in item:
        return item["tok"]
    return round(item["chars"] / chars_per_token) if chars_per_token else 0


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


# The categories /context shows, in its order, so the page and /context can be lined up.
CATEGORIES = ["System prompt", "System tools", "MCP tools", "MCP server instructions", "Memory files", "Skills", "Messages"]
LISTING_CATEGORIES = {"MCP servers": "MCP server instructions", "skills": "Skills"}
MEMORY_RE = re.compile(r"^Contents of \S+ \(.*instructions", re.M)


def categories(root: dict, store: Store, chars_per_token: float) -> list[dict]:
    """The prompt's tokens in /context's categories, each with the path of its first part.

    Like /context, Messages is what the other categories leave of the messages: the conversation,
    and the listings and reminders Claude Code puts into it."""
    totals: dict[str, int] = {}
    paths: dict[str, str] = {}

    def tok(item: dict) -> int:
        return tokens(item, chars_per_token)

    def add(category: str, tokens: int, path: str) -> None:
        totals[category] = totals.get(category, 0) + tokens
        paths.setdefault(category, path)

    def walk(item: dict, category: str) -> None:
        own = LISTING_CATEGORIES.get(item["label"]) if item["kind"] == "listing" else None
        text = store.items.get(item.get("text") or "") if not item["children"] else None
        if own is None and text and MEMORY_RE.search(text):
            own = "Memory files"
        if own:
            add(own, tok(item), item["path"])
            return
        # The part of a node its children do not cover, such as JSON around them, stays in its category.
        add(category, tok(item) - sum(tok(child) for child in item["children"]), item["path"])
        for child in item["children"]:
            walk(child, category)

    for part in root["children"]:
        if part["kind"] == "answer":
            continue
        if part["label"] == "tools":
            add("System tools", tok(part) - sum(tok(tool) for tool in part["children"]), part["path"])
            for tool in part["children"]:
                add("MCP tools" if tool["kind"] == "server" else "System tools", tok(tool), part["path"])
        else:
            walk(part, "System prompt" if part["label"] == "system" else "Messages")
    return [{"label": c, "tok": totals[c], "path": paths[c]} for c in CATEGORIES if totals.get(c, 0) > 0]


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


def new_parts(exchange: dict, previous: dict | None, root: dict) -> list[str]:
    """The paths of the parts a request adds to the previous one in its chain: the messages after the
    ones they share, and tools and system blocks the previous one did not have. A cache_control that
    moved does not make a part new. None for the first request of a chain."""
    if previous is None:
        return []
    by_path = {part["path"]: part for part in root["children"]}
    paths = [m["path"] for m in by_path["messages"]["children"][exchange["kept"]:]]
    added = set(exchange["tools_added"])
    for tool in by_path["tools"]["children"]:
        for item in tool["children"] if tool["kind"] == "server" else [tool]:
            if item["label"] in added:
                paths.append(item["path"])
    if exchange["system_changed"]:
        old = {canon(block) for block in previous["system"][0]}
        for item, block in zip(by_path["system"]["children"], system_blocks(exchange["body"])):
            if canon(block) not in old:
                paths.append(item["path"])
    return paths


def request_tree(exchanges: list[dict], position: int, store: Store, names: dict[str, str], chains: dict) -> dict:
    """The tree of one request. Parts equal to the previous request's in the same chain are that
    request's nodes, shared: chains holds the last request's parts by chain."""
    exchange = exchanges[position]
    body = exchange["body"]
    record = exchange["record"]
    # Numbered among the requests, not among all calls: token counts and other calls are left out.
    root = node(f"request {position + 1}", "request", wire_size(body), ["request"], store)
    servers = {key: names.get(key, key) for key in mcp_servers(exchange)}
    previous = chains.get(exchange["chain_key"])
    last = previous or {}
    if last.get("servers") != servers:
        last = {}  # the listings in a part depend on the servers
    blocks, tools, messages = system_blocks(body), body.get("tools") or [], body.get("messages") or []
    system = last["system"][1] if last.get("system", (None,))[0] == blocks else system_tree(blocks, store, servers)
    tool_part = last["tools"][1] if last.get("tools", (None,))[0] == tools else tools_tree(tools, store, servers)
    message_part = messages_tree(messages, store, servers, last.get("messages") or [])
    chains[exchange["chain_key"]] = {
        "servers": servers,
        "system": (blocks, system),
        "tools": (tools, tool_part),
        "messages": list(zip(messages, message_part["children"])),
    }
    parts = [system, tool_part, message_part]
    other = {k: v for k, v in body.items() if k not in ("system", "tools", "messages")}
    envelope = root["chars"] - sum(part["chars"] for part in parts)
    parts.append(node("envelope: " + ", ".join(other), "envelope", envelope, ["envelope"], store, text=pretty(other)))
    root["children"] = parts
    _, segment_count = segments(root)

    ratio, ratio_source = chars_per_token(exchanges, position)
    answer = answer_tree(exchange["answer"], store)
    estimate_answer(answer, exchange["row"]["output"], ratio)
    root["children"].append(answer)

    row = exchange["row"]
    root["info"] = {
        "index": record["index"],
        "session": session_id(record),
        "chain": exchange["chain"],
        "first_in_chain": exchange["first_in_chain"],
        "new": new_parts(exchange, previous, root),
        "model": body.get("model", "?"),
        "prompt_in": row["prompt_in"],
        "fresh": row["fresh"],
        "cache_read": row["cache_read"],
        "cache_write": row["cache_write"],
        "output": row["output"],
        "duration_ms": record.get("duration_ms", 0),
        "segments": segment_count,
        "ratio": ratio,
        "ratio_source": ratio_source,
        "aborted": bool(record.get("aborted")),
        "status": record.get("status"),
        "stop_reason": exchange["answer"].get("stop_reason"),
        "categories": categories(root, store, ratio),
    }
    return root


COMMAND_RE = re.compile(r"<command-name>/(\S+?)</command-name>")


def session_usage(exchanges: list[dict]) -> dict:
    """How often the session used each item the page offers to tick, by its pick key ("tool: Bash"):
    tools Claude called, skills it ran or the user typed as /NAME, subagent types it started, and MCP
    servers whose tools it called. A deferred tool that ToolSearch loaded but Claude never called
    counts as 0. Every request replays the conversation, so a call counts once, by its id."""
    calls: dict[str, dict] = {}
    commands: set[tuple[int, int, str]] = set()
    for exchange in exchanges:
        for i, message in enumerate(exchange["messages"]):
            content = message.get("content")
            for block in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
                if block.get("type") == "tool_use":
                    calls[block.get("id", "")] = block
                elif block.get("type") == "text" and message.get("role") == "user":
                    for name in COMMAND_RE.findall(block.get("text", "")):
                        commands.add((exchange["chain"], i, name))
        for block in exchange["answer"]["blocks"]:
            if block.get("type") == "tool_use":
                calls[block.get("id", "")] = block

    usage: dict[str, int] = {}

    def count(key: str) -> None:
        usage[key] = usage.get(key, 0) + 1

    for block in calls.values():
        name, args = block.get("name", ""), block.get("input") or {}
        count(f"{tool_kind(name)}: {name}")
        if name.startswith("mcp__"):
            count(f"MCP server: {name.split('__')[1]}")
        elif name == "Skill" and args.get("skill"):
            count(f"skill: {args['skill']}")
        elif name in AGENT_TOOLS and args.get("subagent_type"):
            count(f"subagent: {args['subagent_type']}")
        elif name == "ToolSearch" and str(args.get("query", "")).startswith("select:"):
            for loaded in str(args["query"])[len("select:") :].split(","):
                usage.setdefault(f"{tool_kind(loaded.strip())}: {loaded.strip()}", 0)
    for _, _, name in commands:
        count(f"skill: {name}")
    return usage


def tool_input(block: dict) -> dict:
    """A tool call's input. A streamed answer holds it as the JSON text that arrived."""
    if block.get("input"):
        return block["input"]
    try:
        parsed = json.loads(block.get("partial_json") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def first_text(exchange: dict) -> str:
    messages = exchange["messages"]
    content = messages[0].get("content") if messages else ""
    return content if isinstance(content, str) else "\n".join(block_text(block) for block in content or [])


def chain_links(exchanges: list[dict]) -> list[dict]:
    """One entry per conversation chain, by chain number: its kind, its lane on the timeline and, for a
    subagent, the request whose answer started it and the one that got its result back.

    A subagent's first message holds the prompt its Agent call gave it, which is how the two are
    matched. A chain without tools is a side call: the quota probe, a title. The rest are the main
    conversation, which starts over after a compaction or /clear."""
    chains: dict[int, dict] = {}
    for i, exchange in enumerate(exchanges):
        chain = chains.setdefault(exchange["chain"], {
            "chain": exchange["chain"], "first": i, "requests": 0, "kind": "main" if exchange["tools"] else "side"})
        chain["last"] = i
        chain["requests"] += 1
    for position, exchange in enumerate(exchanges):
        for block in exchange["answer"]["blocks"]:
            if block.get("type") != "tool_use" or block.get("name") not in AGENT_TOOLS:
                continue
            args = tool_input(block)
            prompt = str(args.get("prompt") or "").strip()
            for chain in chains.values():
                opens = first_text(exchanges[chain["first"]])
                if chain["kind"] == "main" and chain["first"] > position and prompt and prompt in opens:
                    chain.update(kind="agent", spawn=position, call=block.get("id", ""),
                                 agent=args.get("subagent_type") or "general-purpose",
                                 task=args.get("description") or "")
                    break
    for chain in chains.values():
        if chain["kind"] != "agent":
            continue
        # The result comes back in a tool result, or later in a notification for an agent that ran
        # in the background: the first request of the parent chain after the subagent's last one
        # whose new messages name the call.
        parent = exchanges[chain["spawn"]]["chain"]
        chain["back"] = next((p for p in range(chain["last"] + 1, len(exchanges)) if exchanges[p]["chain"] == parent
                              and chain["call"] in json.dumps(exchanges[p]["messages"][exchanges[p]["kept"]:])), None)
    # One lane per chain, in the order of a tree: the main conversation on top, each subagent under
    # the chain that started it, side calls at the bottom.
    order: list[dict] = []

    def place(chain: dict, depth: int, lane: int | None = None) -> None:
        if lane is None:
            lane = len(order)
            order.append(chain)
        chain["lane"], chain["depth"] = lane, depth
        for child in chains.values():
            if child["kind"] == "agent" and exchanges[child["spawn"]]["chain"] == chain["chain"]:
                place(child, depth + 1)

    # The main conversation keeps one lane when it starts over; a gap marks where.
    for i, chain in enumerate(c for c in chains.values() if c["kind"] == "main"):
        place(chain, 0, 0 if i else None)
    lanes = len(order)
    for chain in chains.values():
        if chain["kind"] == "side":
            chain["lane"], chain["depth"] = lanes, 0
    return [chains[number] for number in sorted(chains)]


# The argument Claude Code shows in a tool call's line, first found wins: Bash(ls), Read(cli.py).
MAIN_ARGS = ("command", "file_path", "pattern", "url", "query", "skill", "description", "path", "prompt")


# How much of a text a session step shows: its first line, cut to this.
STEP_LIMIT = 160


def call_label(name: str, args: dict) -> str:
    """A tool call as Claude Code shows it: Bash(ls -la), Explore(Report hello.py output)."""
    if name in AGENT_TOOLS:
        name = args.get("subagent_type") or name
    elif name.startswith("mcp__"):
        name = " - ".join(name.split("__")[1:3])
    main = next((args[key] for key in MAIN_ARGS if isinstance(args.get(key), str) and args[key].strip()), None)
    if main is None:
        main = next((value for value in args.values() if isinstance(value, str) and value.strip()), "")
    return f"{name}({shorten(first_line(main), 80)})"


def prompt_text(message: dict) -> str | None:
    """What the user typed, from a user message without tool results. Claude Code adds its reminders
    as blocks of their own, in tags; the user's text is the last block outside them."""
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    if message.get("role") != "user" or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return None
    for block in reversed(blocks):
        text = (block.get("text") or "").strip() if isinstance(block, dict) and block.get("type") == "text" else ""
        command = COMMAND_RE.search(text)
        if command:
            return "/" + command.group(1)
        if text and not text.startswith("<"):
            return text
    return None


def started(exchange: dict) -> float | None:
    try:
        return datetime.fromisoformat(exchange["record"].get("started_at", "")).timestamp()
    except (TypeError, ValueError):
        return None


def said(message: dict) -> tuple:
    """A message's role and text. Claude Code writes a message again in another form once its cache
    marker moves on, a list of blocks becoming a string, so the form does not count."""
    content = message.get("content")
    blocks = [] if isinstance(content, str) else content or []
    text = content if isinstance(content, str) else "\n".join(
        block_text({k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block)
        for block in blocks)
    return message.get("role"), text.strip()


def side_questions(exchanges: list[dict]) -> set[int]:
    """The requests Claude Code sends on the side of a conversation: the conversation so far plus a
    question of its own, such as a suggested next prompt or a recap after you were away. Claude Code
    does not show them, and the conversation goes on without them.

    The next request of the chain has another message where the question was. The last request of a
    chain has no next one; it is a side question when it started within a second of an answer that
    had ended its turn, faster than anyone types, and called no tools."""
    side: set[int] = set()
    by_chain: dict[int, list[int]] = {}
    for position, exchange in enumerate(exchanges):
        by_chain.setdefault(exchange["chain"], []).append(position)
    for positions in by_chain.values():
        for k, position in enumerate(positions):
            messages = exchanges[position]["messages"]
            if not messages:
                continue
            if k + 1 < len(positions):
                later = exchanges[positions[k + 1]]["messages"]
                if len(later) < len(messages) or said(later[len(messages) - 1]) != said(messages[-1]):
                    side.add(position)
            elif k:
                before, now = exchanges[positions[k - 1]], exchanges[position]
                start, end = started(now), started(before)
                calls = any(block.get("type") == "tool_use" for block in now["answer"]["blocks"])
                if (before["answer"].get("stop_reason") == "end_turn" and not calls and start is not None
                        and end is not None and start - end - before["record"].get("duration_ms", 0) / 1000 < 1):
                    side.add(position)
    return side


def session_steps(exchanges: list[dict], links: list[dict]) -> list[dict]:
    """The session as Claude Code shows it: what the user typed, Claude's text, its tool calls with the
    first line of their results, and each subagent's steps under the Agent call that started it. A
    step names its request and the path of its part there, so the page can select it."""
    results: dict[str, tuple[str, bool]] = {}
    for exchange in exchanges:
        for message in exchange["messages"][exchange["kept"]:]:
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    line = shorten(first_line(block_text(block)), STEP_LIMIT)
                    results.setdefault(block.get("tool_use_id", ""), (line, bool(block.get("is_error"))))
    agents = {chain["call"]: chain for chain in links if chain["kind"] == "agent"}
    branch = side_questions(exchanges)

    def steps_of(chain: dict) -> list[dict]:
        steps: list[dict] = []
        typed: set[tuple[int, str]] = set()
        for position in range(chain["first"], chain["last"] + 1):
            exchange = exchanges[position]
            if exchange["chain"] != chain["chain"] or position in branch:
                continue
            # A subagent's first message is the prompt its Agent call shows already.
            for i in range(exchange["kept"], len(exchange["messages"])) if chain["kind"] == "main" else ():
                text = prompt_text(exchange["messages"][i])
                if text and (i, text) not in typed:
                    typed.add((i, text))
                    steps.append({"kind": "prompt", "req": position, "path": f"messages/{i}",
                                  "label": shorten(first_line(text), STEP_LIMIT)})
            for j, block in enumerate(exchange["answer"]["blocks"]):
                step = {"req": position, "path": f"answer/{j}"}
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    step.update(kind="text", label=shorten(first_line(block["text"]), STEP_LIMIT))
                elif block.get("type") in ("tool_use", "server_tool_use"):
                    step.update(kind="tool", label=call_label(block.get("name", "?"), tool_input(block)))
                    call = block.get("id", "")
                    if call in agents:
                        sub = agents[call]
                        done = "Done" if sub["back"] is not None else "No result yet"
                        count = f"{sub['requests']} request{'' if sub['requests'] == 1 else 's'}"
                        step.update(kind="agent", children=steps_of(sub), result=f"{done} \u00b7 {count}")
                    elif call in results:
                        step["result"], step["error"] = results[call]
                else:
                    continue
                steps.append(step)
        return steps

    session: list[dict] = []
    for chain in links:
        if chain["kind"] != "main":
            continue
        if session:
            session.append({"kind": "restart", "req": chain["first"], "path": "request",
                            "label": "New conversation, after /clear or a compaction"})
        session.extend(steps_of(chain))
    return session


def build_profile(exchanges: list[dict]) -> dict:
    store = Store()
    names = server_names(exchanges)
    chains: dict = {}
    requests = [request_tree(exchanges, i, store, names, chains) for i in range(len(exchanges))]
    links = chain_links(exchanges)
    return {"requests": requests, "content": store.items, "usage": session_usage(exchanges),
            "servers": sorted(names.values()), "chains": links, "session": session_steps(exchanges, links)}


# --- the page --------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e0e0e0;
        --card:#fafafa; --code:#f4f4f5; --accent:#7a3fd4; --bar:#c9b3f2; --answer:#0a7b34;
        --sel:#efe7fb; --warn:#b3261e; --shade:rgba(0,0,0,.35); --mark:#ffe27a;
        --write:#e8962e; --fresh:#2f80c8; --link:rgba(47,128,200,.16); }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16161a; --fg:#e6e6e6; --muted:#9a9aa5; --line:#2c2c33; --card:#1d1d22;
          --code:#232329; --accent:#b18cf0; --bar:#4b3a73; --answer:#4ac97e; --sel:#2a2438; --warn:#ff7b72; --shade:rgba(0,0,0,.6); --mark:#6b5a12;
          --write:#c47e22; --fresh:#4a9be0; --link:rgba(74,155,224,.2); }
}
* { box-sizing: border-box; }
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--fg); display:flex; flex-direction:column;
       font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.picker { display:flex; flex-wrap:wrap; gap:.6rem; align-items:center; padding:.5rem 9rem .5rem 1.5rem; border-bottom:1px solid var(--line); }
.picker .step { font:16px/1 ui-monospace, monospace; padding:.1rem .55rem; }
.steps { display:flex; gap:.25rem; }
.tl-label { font:12px ui-monospace, monospace; color:var(--muted); white-space:nowrap; }
.where { display:flex; gap:.5rem; align-items:baseline; font:600 13px ui-monospace, monospace; white-space:nowrap; margin-right:.6rem; }
.where .branch { font-weight:400; color:var(--muted); }
.fold { margin-left:auto; font-size:12px; }
.legend + .fold { margin-left:0; }
.legend { margin-left:auto; display:flex; flex-wrap:wrap; justify-content:flex-end; gap:.9rem; font:12px ui-monospace, monospace; color:var(--muted); white-space:nowrap; }
.charts[hidden] { display:none; }
.charts { display:flex; max-height:40vh; overflow-y:auto; border-bottom:1px solid var(--line); background:var(--card); }
.lane-names { flex:none; width:clamp(9rem, 22vw, 16rem); display:flex; flex-direction:column; gap:3px; padding:6px 0; border-right:1px solid var(--line); }
.lane-name { display:flex; flex-direction:column; justify-content:center; padding:0 .7rem; font:12px/1.25 ui-monospace, monospace;
             color:var(--muted); cursor:pointer; overflow:hidden; }
.lane-name span { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.lane-name small { font-size:11px; opacity:.8; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.lane-name:hover { color:var(--fg); }
.lane-name.on { color:var(--fg); background:var(--sel); }
.timeline { flex:1; min-width:0; display:flex; align-items:flex-start; gap:2px; overflow-x:auto; position:relative; z-index:0; padding:6px .8rem; }
.tl-band { position:absolute; left:0; z-index:-1; background:var(--sel); pointer-events:none; }
.timeline.dense { gap:0; }
.timeline.dense .tl-col { min-width:1px; }
.timeline.dense .tl-bar { border-radius:0; }
.tl-col { flex:1 1 0; min-width:2px; max-width:28px; display:flex; flex-direction:column; gap:3px; border-radius:3px; }
.tl-col.on { background:var(--sel); }
.tl-col.gap { margin-left:7px; }
.tl-cell { display:flex; align-items:flex-end; }
.tl-col.link { background:var(--link); }
.tl-bar { width:100%; background:var(--bar); border-radius:2px 2px 0 0; cursor:pointer;
          display:flex; flex-direction:column; overflow:hidden; }
.tl-bar i { display:block; flex:none; }
.tl-bar:hover { opacity:.7; }
.tl-bar.on { box-shadow:0 0 0 2px var(--fg); }
.tl-read { background:var(--bar); }
.tl-write { background:var(--write); }
.tl-fresh { background:var(--fresh); }
.sw { display:inline-block; width:.65em; height:.65em; border-radius:2px; margin-right:.3em; }
.tl-bar.err { background:var(--warn); }
.tl-bar.miss { opacity:.25; }
button { font:inherit; padding:.3rem .7rem; border:1px solid var(--line); border-radius:6px;
         background:var(--card); color:var(--fg); cursor:pointer; }
button:hover { background:var(--code); }
button:disabled { opacity:.5; cursor:default; }
button.on { background:var(--sel); border-color:var(--accent); color:var(--accent); }
.split { flex:1; display:grid; grid-template-columns:minmax(360px, 44%) 1fr; min-height:0; }
.tree, .pane { overflow:auto; min-height:0; }
.tree { border-right:1px solid var(--line); }
.toolbar button { white-space:nowrap; }
.toolbar { display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; padding:.5rem .8rem; border-bottom:1px solid var(--line);
           position:sticky; top:0; background:var(--bg); z-index:1; font-size:12px; color:var(--muted); }
.toolbar input { flex:1 1 8rem; min-width:6rem; font:inherit; font-size:13px; padding:.25rem .5rem; color:var(--fg);
                 background:var(--bg); border:1px solid var(--line); border-radius:6px; }
.toolbar .found { white-space:nowrap; }
mark { background:var(--mark); color:inherit; border-radius:2px; }
.row { display:grid; grid-template-columns:1fr 6rem 5rem 3.2rem; align-items:center; gap:.5rem;
       padding:.15rem .8rem .15rem 0; cursor:pointer; border-bottom:1px solid transparent; }
.row:hover { background:var(--code); }
.row.sel { background:var(--sel); }
.tabs { display:flex; gap:0; padding:.5rem .8rem 0; border-bottom:1px solid var(--line); }
.tabs button { border:none; border-bottom:2px solid transparent; border-radius:0; background:none; color:var(--muted); padding:.3rem .8rem; }
.tabs button.on { color:var(--fg); border-bottom-color:var(--accent); }
.tree.session .toolbar { display:none; }
.srow { display:grid; grid-template-columns:1.1rem 1fr auto; gap:.2rem .4rem; padding:.3rem .8rem; cursor:pointer; align-items:baseline; }
.srow:hover { background:var(--code); }
.srow.cur { box-shadow:inset 3px 0 0 var(--accent); }
.srow.sel { background:var(--sel); }
.srow:focus { outline:none; }
.srow:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
.srow .mark { color:var(--muted); font:12px ui-monospace, monospace; text-align:center; }
.srow.tool .mark, .srow.agent .mark { color:var(--answer); }
.srow.prompt { background:var(--code); }
.srow.prompt .mark { color:var(--accent); }
.srow .what { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.srow.tool .what, .srow.agent .what { font:12.5px ui-monospace, monospace; }
.srow.agent .what { font-weight:600; }
.srow.restart .what { color:var(--muted); font-style:italic; }
.srow .result { grid-column:2 / 4; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
                color:var(--muted); font:12px ui-monospace, monospace; }
.srow .result.warn { color:var(--warn); }
.srow .req { font:11px ui-monospace, monospace; color:var(--muted); white-space:nowrap; }
.row.around .name { color:var(--muted); }
.note.empty { padding:0 .8rem; }
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
.row .sub { font:11px ui-monospace, monospace; color:var(--accent); flex:0 1000 auto; min-width:0; overflow:hidden;
            text-overflow:ellipsis; cursor:pointer; }
.row .sub:hover { text-decoration:underline; }
.row .new { width:7px; height:7px; border-radius:50%; background:var(--fresh); flex:none; }
.row .used { font:10px ui-monospace, monospace; color:var(--answer); flex:none; }
.row .tick { margin:0; flex:none; cursor:pointer; }
.row:focus, .tl-bar:focus { outline:none; }
.row:focus-visible, .tl-bar:focus-visible, a:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
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
    var content = data.content, usage = data.usage || {};
    // Each node comes once, its children as indexes: a part shared by many requests is one object.
    var nodes = data.nodes;
    nodes.forEach(function (item) { item.children = item.children.map(function (i) { return nodes[i]; }); });
    var requests = data.requests.map(function (i) { return nodes[i]; });
    // The conversation chains by number: the main one, subagents and side calls, each on its lane.
    var chains = data.chains || [], byCall = {};
    chains.forEach(function (c) { if (c.call) byCall[c.call] = c; });
    function chainOf(r) { return chains[r.info.chain - 1] || { kind: 'main', lane: 0 }; }
    function chainName(c) { return c.kind === 'agent' ? c.agent + ' subagent' : c.kind === 'side' ? 'side call' : 'main conversation'; }
    // paneSort is null while the table follows the tree's sort toggle; clicking
    // a column header gives the table an order of its own.
    var state = { view: 'request', timelineOpen: false, req: 0, path: 'request', sortBySize: true, open: {}, jsonOpen: {}, rawView: false, paneSort: null, picked: {}, dialog: false, query: '', onlyNew: false };
    var $ = function (id) { return document.getElementById(id); };
    var n = function (x) { return (x || 0).toLocaleString('en-US'); };
    var pct = function (a, b) { return b ? (100 * a / b).toFixed(1) + '%' : '-'; };
    var el = function (tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    };
    // A link that selects a part: reachable with Tab, and Enter selects and moves to its tree row.
    // With req, it selects that request.
    var link = function (text, path, req, cls) {
        var a = el(cls ? 'span' : 'a', cls || '', text);
        var go = function () { select(req === undefined ? state.req : req, path); };
        a.tabIndex = 0;
        a.onclick = function (ev) { ev.stopPropagation(); go(); };
        a.onkeydown = function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); go(); focusRow(); } };
        return a;
    };
    // The path of the part of a request that carries a subagent's call, for a link to land on it.
    function callPath(r, call) {
        var hit = null;
        (function look(item) {
            if (hit) return;
            if (item.call === call && item.kind === 'tool_use') { hit = item.path; return; }
            item.children.forEach(look);
        })(r);
        return hit || 'request';
    }

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

    // Laid out like a profiler's charts: the toolbar steps through the requests; below it, one row per
    // conversation, named in a column on the left as a tree: the main conversation, each subagent
    // under the conversation that started it, side calls last. One bar per request, its height the
    // prompt's tokens on one scale, so the rows show how each context grew. The selected request is
    // lit down every row. A gap starts a new main conversation; a failed call is red.
    var LANE = 64, MIN_LANE = 18;
    function failed(info) { return info.status >= 400 || info.aborted; }
    function renderPicker() {
        var box = $('picker');
        var barFocused = document.activeElement && document.activeElement.className.indexOf('tl-bar') === 0;
        box.innerHTML = '';
        // Which session this is: the folder Claude ran in and its branch.
        if (data.project) {
            var at = el('span', 'where', data.project.split(/[\\/]/).filter(Boolean).pop() || data.project);
            if (data.branch) at.appendChild(el('span', 'branch', '\u2387 ' + data.branch));
            at.title = data.project + (data.branch ? ' \u00b7 branch ' + data.branch : '');
            box.appendChild(at);
        }
        var info = root().info, own = chainOf(root());
        // The buttons come first, together, so the label's length never moves them.
        var steps = el('span', 'steps'), last = requests.length - 1;
        [['\u00ab', 'first request (Home)', 0, state.req === 0], ['\u2039', 'previous request (\u2190)', state.req - 1, state.req === 0],
         ['\u203a', 'next request (\u2192)', state.req + 1, state.req === last], ['\u00bb', 'last request (End)', last, state.req === last]]
            .forEach(function (b) {
                var button = el('button', 'step', b[0]);
                button.title = b[1];
                button.disabled = b[3];
                button.onclick = function () { select(b[2], state.path); };
                steps.appendChild(button);
            });
        box.appendChild(steps);
        box.appendChild(el('span', 'tl-label', 'request ' + (state.req + 1) + ' of ' + requests.length + ' \u00b7 ' +
            (failed(info) ? (info.status >= 400 ? 'HTTP ' + info.status : 'aborted') : n(info.prompt_in) + ' tokens') +
            ((info.new || []).length ? ' \u00b7 +' + n(root().newTok) + ' new' : '') +
            (own.kind !== 'main' ? ' \u00b7 ' + chainName(own) : '')));
        var legend = el('div', 'legend');
        [['tl-fresh', 'fresh'], ['tl-write', 'cache write'], ['tl-read', 'cache read']].forEach(function (pair) {
            var item = el('span');
            item.appendChild(el('i', 'sw ' + pair[0]));
            item.appendChild(document.createTextNode(pair[1]));
            legend.appendChild(item);
        });
        if (state.timelineOpen) box.appendChild(legend);
        // The rows fold away, for more room below; the buttons still step through the requests.
        var fold = el('button', 'fold', (!state.timelineOpen ? '\u25b8' : '\u25be') + ' timeline');
        fold.title = state.timelineOpen ? 'hide the timeline' : 'show the timeline';
        fold.setAttribute('aria-expanded', state.timelineOpen ? 'true' : 'false');
        fold.onclick = function () {
            state.timelineOpen = !state.timelineOpen;
            renderPicker();
            persist();
            $('picker').querySelector('.fold').focus();
        };
        box.appendChild(fold);
        renderCharts(barFocused);
    }

    function renderCharts(barFocused) {
        var charts = $('charts');
        var scrollTop = charts.scrollTop;
        charts.innerHTML = '';
        // Folded by default: the toolbar's buttons step through the requests without it.
        charts.hidden = !state.timelineOpen;
        if (!state.timelineOpen) return;
        var top = Math.max.apply(null, requests.map(function (r) { return r.info.prompt_in; }).concat([1]));
        var height = function (r) { return Math.max(4, LANE * r.info.prompt_in / top); };
        // Each row is as tall as its tallest bar, and tall enough for its name.
        var lanes = [], named = [], counts = [];
        requests.forEach(function (r) {
            var c = chainOf(r);
            while (lanes.length <= c.lane) { lanes.push(MIN_LANE); counts.push(0); }
            lanes[c.lane] = Math.max(lanes[c.lane], height(r));
            counts[c.lane]++;
            if (!named[c.lane]) named[c.lane] = c;
        });
        var names = el('div', 'lane-names');
        lanes.forEach(function (h, lane) {
            var c = named[lane];
            var row = el('div', 'lane-name' + (chainOf(root()).lane === lane ? ' on' : ''));
            row.style.height = h + 'px';
            row.style.paddingLeft = (0.7 + 1.1 * (c.depth || 0)) + 'rem';
            var text = c.kind === 'agent' ? '\u23bf ' + c.agent + (c.task ? ' \u00b7 ' + c.task : '') :
                       c.kind === 'side' ? 'Side calls' : 'Main conversation';
            row.appendChild(el('span', '', text));
            if (h >= 30) row.appendChild(el('small', '', counts[lane] + ' request' + (counts[lane] === 1 ? '' : 's') +
                                            (lane === 0 ? ' \u00b7 tallest bar ' + n(top) : '')));
            row.title = text + ' \u00b7 ' + counts[lane] + ' requests';
            row.onclick = function () { select(c.first, 'request'); };
            names.appendChild(row);
        });
        charts.appendChild(names);
        var strip = el('div', 'timeline' + (requests.length > 120 ? ' dense' : ''));
        // The requests linked to the selected one are tinted: for a subagent's request, the request
        // that started it and the one that got its result; for a request that started subagents,
        // their first requests and the ones that got their results.
        var linked = {}, own = chainOf(root());
        chains.forEach(function (c) {
            if (c.kind !== 'agent' || (c !== own && c.spawn !== state.req)) return;
            linked[c === own ? c.spawn : c.first] = true;
            if (c.back !== null) linked[c.back] = true;
        });
        var q = query(), mainSeen = false;
        requests.forEach(function (r, i) {
            var c = chainOf(r);
            var col = el('div', 'tl-col' + (i === state.req ? ' on' : linked[i] ? ' link' : '') +
                         (c.kind === 'main' && r.info.first_in_chain && mainSeen ? ' gap' : ''));
            if (c.kind === 'main') mainSeen = true;
            var bar = el('div', 'tl-bar' + (i === state.req ? ' on' : '') + (failed(r.info) ? ' err' : '') +
                         (q && !search(r, q, {}) ? ' miss' : ''));
            bar.style.height = height(r) + 'px';
            bar.title = 'request ' + (i + 1) + ' \u00b7 ' + n(r.info.prompt_in) + ' tokens \u00b7 ' + r.info.model +
                        ' \u00b7 ' + chainName(c) + (c.task ? ': ' + c.task : '') + (failed(r.info) ? ' \u00b7 failed' :
                        ' \u00b7 cache read ' + n(r.info.cache_read) + ', written ' + n(r.info.cache_write) +
                        ', fresh ' + n(r.info.fresh));
            // The bar is split by the API's own counts: fresh and written to the cache on top, read
            // from the cache below. A bright top is what this request added, or a cache that broke.
            if (!failed(r.info) && r.info.prompt_in) {
                [['tl-fresh', r.info.fresh], ['tl-write', r.info.cache_write]].forEach(function (part) {
                    var piece = el('i', part[0]);
                    piece.style.height = (100 * part[1] / r.info.prompt_in) + '%';
                    bar.appendChild(piece);
                });
            }
            bar.onclick = function () { select(i, state.path); };
            // One bar takes Tab, the selected one; the arrow keys step through the requests.
            bar.tabIndex = i === state.req ? 0 : -1;
            bar.setAttribute('role', 'button');
            bar.setAttribute('aria-label', bar.title);
            lanes.forEach(function (h, lane) {
                var cell = el('div', 'tl-cell');
                cell.style.height = h + 'px';
                if (lane === c.lane) cell.appendChild(bar);
                col.appendChild(cell);
            });
            strip.appendChild(col);
        });
        charts.appendChild(strip);
        // The selected conversation's row is lit across the bars too, behind them, in line with its
        // name on the left.
        if (strip.firstChild) {
            var cell = strip.firstChild.children[chainOf(root()).lane];
            var band = el('div', 'tl-band');
            band.style.top = cell.offsetTop + 'px';
            band.style.height = cell.offsetHeight + 'px';
            band.style.width = strip.scrollWidth + 'px';
            strip.insertBefore(band, strip.firstChild);
        }
        charts.scrollTop = scrollTop;
        // In a narrow window the strip scrolls; keep the selected bar in view.
        var on = strip.querySelector('.tl-col.on');
        if (on && strip.scrollWidth > strip.clientWidth) {
            strip.scrollLeft = on.offsetLeft - strip.clientWidth / 2;
        }
        var bar = on && on.querySelector('.tl-bar');
        if (barFocused && bar) bar.focus();
    }

    function renderTree() {
        document.querySelector('.tree').className = 'tree' + (state.view === 'session' ? ' session' : '');
        $('tab-request').className = state.view === 'request' ? 'on' : '';
        $('tab-session').className = state.view === 'session' ? 'on' : '';
        $('tab-request').setAttribute('aria-selected', state.view === 'request' ? 'true' : 'false');
        $('tab-session').setAttribute('aria-selected', state.view === 'session' ? 'true' : 'false');
        if (state.view === 'session') { renderSession(); return; }
        $('rows').setAttribute('role', 'tree');
        var box = $('rows');
        box.innerHTML = '';
        var r = root(), q = query(), hits = null;
        if (q) {
            hits = {};
            search(r, q, hits);
            var count = Object.keys(hits).filter(function (p) { return hits[p] === 'hit'; }).length;
            $('found').textContent = count === 1 ? '1 part' : count + ' parts';
        } else {
            $('found').textContent = '';
        }
        var walk = function (item, depth, total, answer, parentSeg, inHit) {
            if (hits && !inHit && !hits[item.path]) return;
            // While searching, the parts around a hit are open and dim, so the hits stand out. A hit
            // opens like any part, and what is inside it shows dim.
            var rel = newness(item.path);
            if (state.onlyNew && !rel) return;
            // With "only new", the parts around a new part are open and dim too.
            var around = hits && (inHit || hits[item.path] === 'around') || state.onlyNew && rel === 'around';
            var open = hits && !inHit && hits[item.path] === 'around' || state.onlyNew && rel === 'around' ? true : isOpen(item, depth);
            var row = el('div', 'row' + (item.path === state.path ? ' sel' : '') + (answer ? ' answer' : '') +
                         (around ? ' around' : ''));
            row.style.paddingLeft = (0.4 + depth * 1.1) + 'rem';
            var lbl = el('div', 'lbl');
            var caret = el('span', 'caret', item.children.length ? (open ? '▾' : '▸') : '');
            caret.onclick = function (ev) { ev.stopPropagation(); state.open[item.path] = !isOpen(item, depth); renderTree(); persist(); };
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
            var sub = item.kind === 'tool_use' && byCall[item.call];
            if (sub) {
                var go = link('\u2192 ' + sub.agent + (sub.task ? ': ' + sub.task : '') + ' (' + sub.requests + ' request' +
                              (sub.requests === 1 ? '' : 's') + ')', 'request', sub.first, 'sub');
                go.title = 'go to the first request of this subagent';
                lbl.appendChild(go);
            }
            if (rel === 'new') {
                var dot = el('span', 'new');
                dot.title = 'new since the previous request';
                dot.setAttribute('aria-label', dot.title);
                lbl.appendChild(dot);
            }
            var used = usage[item.pick ? usageKey(item.pick) : 'MCP tool: ' + item.label];
            if (used !== undefined) lbl.appendChild(el('span', 'used', used ? 'used ' + used + '\u00d7' : 'loaded'));
            if (item.seg !== undefined && item.seg !== parentSeg) lbl.appendChild(el('span', 'seg', 'seg ' + (item.seg + 1)));
            row.appendChild(lbl);
            var bar = el('div', 'bar'); var fill = el('i');
            fill.style.width = (total ? Math.max(0.5, 100 * item.tok / total) : 0) + '%';
            bar.appendChild(fill); row.appendChild(bar);
            var num = el('div', 'num'); num.innerHTML = '<b>' + n(item.tok) + '</b>';
            row.appendChild(num);
            row.appendChild(el('div', 'num', pct(item.tok, total)));
            row.onclick = function () { select(state.req, item.path); };
            // One row takes Tab, the selected one; rowKey moves through the tree from there.
            row.setAttribute('data-path', item.path);
            row.tabIndex = item.path === state.path ? 0 : -1;
            row.setAttribute('role', 'treeitem');
            row.setAttribute('aria-level', depth + 1);
            row.setAttribute('aria-selected', item.path === state.path ? 'true' : 'false');
            if (item.children.length) row.setAttribute('aria-expanded', open ? 'true' : 'false');
            row.onkeydown = function (ev) { rowKey(ev, item, open); };
            box.appendChild(row);
            if (!item.children.length || !open) return;
            var kids = item.children.slice();
            if (state.sortBySize) kids.sort(function (a, b) { return b.tok - a.tok; });
            kids.forEach(function (k) {
                var isAnswer = answer || k.kind === 'answer';
                walk(k, depth + 1, isAnswer ? answerOf(r).tok : total, isAnswer, item.seg,
                     inHit || (hits && hits[item.path] === 'hit'));
            });
        };
        walk(r, 0, r.tok, false, undefined);
        if (q) highlight(box, q);
        if (box.childNodes.length) return;
        box.appendChild(el('p', 'note empty', q ? 'No part of this request contains \u201c' + state.query.trim() + '\u201d.' :
            r.info.first_in_chain ? 'The first request of its conversation: there is no previous one to compare with.' :
            'This request adds nothing to the previous one.'));
    }

    // --- the session -----------------------------------------------------
    // The session as Claude Code shows it: what you typed, Claude's text and tool calls with the first
    // line of their results, each subagent's steps under the Agent call that started it. A step
    // selects the part of the request it came from; the steps of the request on show are marked.
    function renderSession() {
        var box = $('rows');
        box.innerHTML = '';
        box.setAttribute('role', 'list');
        $('found').textContent = '';
        var last = -1, focus = null;
        var walk = function (step, depth) {
            var on = step.req === state.req && step.path === state.path;
            var row = el('div', 'srow ' + step.kind + (step.req === state.req ? ' cur' : '') + (on ? ' sel' : ''));
            row.style.paddingLeft = (0.8 + 1.4 * depth) + 'rem';
            row.appendChild(el('span', 'mark', step.kind === 'prompt' ? '>' : step.kind === 'restart' ? '\u2014' : '\u23fa'));
            row.appendChild(el('span', 'what', step.label));
            // The request a step came from, once for each run of steps from the same request.
            var r = requests[step.req];
            var req = el('span', 'req', step.req === last ? '' : '#' + (step.req + 1) + ' \u00b7 ' + n(r.info.prompt_in));
            req.title = 'request ' + (step.req + 1) + ', ' + n(r.info.prompt_in) + ' tokens';
            row.appendChild(req);
            last = step.req;
            if (step.result) row.appendChild(el('span', 'result' + (step.error ? ' warn' : ''), '\u23bf  ' + step.result));
            row.title = step.label;
            row.setAttribute('role', 'listitem');
            row.tabIndex = -1;
            row.onclick = function () { select(step.req, step.path); };
            row.onkeydown = function (ev) { stepKey(ev, step); };
            if (on || (!focus && step.req === state.req)) focus = row;
            box.appendChild(row);
            (step.children || []).forEach(function (child) { walk(child, depth + 1); });
        };
        (data.session || []).forEach(function (step) { walk(step, 0); });
        if (!box.childNodes.length) {
            box.appendChild(el('p', 'note empty', 'No conversation yet: only side calls so far.'));
            return;
        }
        // One step takes Tab: the selected one, or the first of the request on show.
        focus = focus || box.firstChild;
        focus.tabIndex = 0;
        var tree = document.querySelector('.tree');
        if (focus.offsetTop < tree.scrollTop || focus.offsetTop > tree.scrollTop + tree.clientHeight - 40) {
            focus.scrollIntoView({ block: 'center' });
        }
    }
    // Up and down move through the steps and select each.
    function stepKey(ev, step) {
        var rows = [].slice.call($('rows').querySelectorAll('.srow'));
        var at = rows.indexOf(ev.currentTarget), go = null;
        if (ev.key === 'ArrowDown') go = rows[at + 1];
        else if (ev.key === 'ArrowUp') go = rows[at - 1];
        else if (ev.key === 'Enter') go = ev.currentTarget;
        else return;
        ev.preventDefault();
        ev.stopPropagation();
        if (go) { go.click(); var now = $('rows').querySelector('.srow.sel'); if (now) now.focus(); }
    }
    function showView(view) {
        state.view = view;
        renderTree();
        persist();
    }

    // --- search -----------------------------------------------------------
    // The search keeps the parts whose name or text contains the query, and the parts around them.
    // A part is a hit when no part inside it is one, so a message is not counted again for the block
    // inside it. Each text is lowercased once, and checked once per query.
    var lower = {}, holds = {}, held = '';
    function query() { return state.query.trim().toLowerCase(); }
    function contains(key, q) {
        if (!key) return false;
        if (held !== q) { holds = {}; held = q; }
        if (!(key in holds)) {
            if (!(key in lower)) lower[key] = content[key].toLowerCase();
            holds[key] = lower[key].indexOf(q) >= 0;
        }
        return holds[key];
    }
    function search(item, q, out) {
        var inside = false;
        item.children.forEach(function (k) { if (search(k, q, out)) inside = true; });
        var own = item.label.toLowerCase().indexOf(q) >= 0 || contains(item.text, q) || contains(item.raw, q);
        if (inside) out[item.path] = 'around'; else if (own) out[item.path] = 'hit';
        return inside || own;
    }
    // Wrap each occurrence in <mark>, splitting the text nodes; the text stays textContent.
    function highlight(box, q) {
        var walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT), nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach(function (node) {
            var text = node.nodeValue, low = text.toLowerCase(), at = low.indexOf(q), from = 0;
            if (at < 0 || low.length !== text.length) return;
            var frag = document.createDocumentFragment();
            while (at >= 0) {
                frag.appendChild(document.createTextNode(text.slice(from, at)));
                frag.appendChild(el('mark', '', text.slice(at, at + q.length)));
                from = at + q.length;
                at = low.indexOf(q, from);
            }
            frag.appendChild(document.createTextNode(text.slice(from)));
            node.parentNode.replaceChild(frag, node);
        });
    }
    function setQuery(value) {
        state.query = value;
        renderPicker(); renderTree(); renderPane();
        persist();
    }

    // The tree from the keyboard: up and down move the selection, right opens a part or steps into
    // it, left closes it or steps out to its parent, Enter selects, and space ticks the checkbox.
    function rowKey(ev, item, open) {
        var rows = [].slice.call($('rows').querySelectorAll('.row'));
        var at = rows.indexOf(ev.currentTarget), go = null;
        if (ev.key === 'ArrowDown') go = rows[at + 1];
        else if (ev.key === 'ArrowUp') go = rows[at - 1];
        else if (ev.key === 'ArrowRight' && item.children.length && !open) state.open[item.path] = true;
        else if (ev.key === 'ArrowRight') go = item.children.length ? rows[at + 1] : null;
        else if (ev.key === 'ArrowLeft' && item.children.length && open) state.open[item.path] = false;
        else if (ev.key === 'ArrowLeft') go = { path: (parentOf(root(), item.path) || {}).path };
        else if (ev.key === 'Enter') go = { path: item.path };
        else if (ev.key === ' ' && item.pick) togglePick(item.pick, !state.picked[pickKey(item.pick)]);
        else return;
        ev.preventDefault();
        ev.stopPropagation();
        if (go && (go.path || go.getAttribute)) select(state.req, go.path || go.getAttribute('data-path'));
        else { renderTree(); persist(); }
        focusRow();
    }
    function focusRow() {
        var row = $('rows').querySelector('.row[tabindex="0"]');
        if (row) row.focus();
    }

    function crumbs(item) {
        var box = el('div', 'crumbs');
        var parts = item.path === 'request' ? [] : item.path.split('/');
        var paths = ['request'];
        for (var i = 0; i < parts.length; i++) paths.push(parts.slice(0, i + 1).join('/'));
        paths.forEach(function (p, i) {
            var hit = find(root(), p);
            var a = link(hit ? hit.label : p, p);
            if (i) box.appendChild(document.createTextNode(' › '));
            box.appendChild(a);
        });
        return box;
    }

    function fact(box, label, value, cls, swatch) {
        var s = el('span', cls || '');
        if (swatch) s.appendChild(el('i', 'sw ' + swatch));
        s.appendChild(document.createTextNode(label + ' '));
        s.appendChild(el('b', '', value));
        box.appendChild(s);
    }

    // The session ID and the recording's folder, to paste into a terminal or a bug report.
    function ids(info) {
        var box = el('div', 'ids');
        [['session', info.session], ['project', data.project], ['recording', data.run_dir]].forEach(function (pair) {
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

    // The request in /context's categories, so the page and /context can be lined up; the tree on
    // the left already shows the request's own parts.
    var CATEGORY_NOTES = {
        'System tools': '/context shows this less Skills, and adds the difference to Messages',
        'Messages': 'the conversation, and the listings and reminders Claude Code puts into it'
    };
    function categoryTable(info) {
        var t = el('table'), head = el('tr');
        ['as /context counts it', '\u2248 tokens', 'share'].forEach(function (h) { head.appendChild(el('th', '', h)); });
        t.appendChild(head);
        info.categories.forEach(function (c) {
            var tr = el('tr'), td = el('td'), a = link(c.label, c.path);
            if (CATEGORY_NOTES[c.label]) td.title = CATEGORY_NOTES[c.label];
            td.appendChild(a); tr.appendChild(td);
            tr.appendChild(el('td', '', n(c.tok)));
            tr.appendChild(el('td', '', pct(c.tok, info.prompt_in)));
            t.appendChild(tr);
        });
        return t;
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
            var own = chainOf(r);
            if (own.kind === 'agent') {
                var from = el('span', '', own.agent + ' subagent \u00b7 started by ');
                from.appendChild(link('request ' + (own.spawn + 1), callPath(requests[own.spawn], own.call), own.spawn));
                if (own.back !== null) {
                    from.appendChild(document.createTextNode(', result back in '));
                    from.appendChild(link('request ' + (own.back + 1), 'request', own.back));
                }
                facts.appendChild(from);
            }
            fact(facts, 'model', info.model);
            fact(facts, 'prompt', n(info.prompt_in) + ' tokens');
            fact(facts, 'fresh', n(info.fresh), '', 'tl-fresh');
            fact(facts, 'cache read', n(info.cache_read), '', 'tl-read');
            fact(facts, 'cache write', n(info.cache_write), '', 'tl-write');
            if ((info.new || []).length) fact(facts, 'new since the previous request', '+' + n(r.newTok));
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
        if (item.kind === 'request') {
            pane.appendChild(ids(info));
            pane.appendChild(categoryTable(info));
        } else if (item.children.length) {
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
                var td = el('td'); var a = link(k.label, k.path);
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
        if (query()) highlight(pane, query());
    }

    // --- readable raw ------------------------------------------------------
    // The exact JSON of a part, every key in its order, rendered to read:
    // dim keys, long strings as formatted text, a token estimate per object
    // and long string. Recorded text is untrusted, so every node is built with
    // textContent.
    function jsonView(value, base) {
        var ratio = root().info.ratio || 4, q = query();
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
            if (o !== undefined) return o;
            return depth === 0 || tokens(v) <= 1500 || (!!q && JSON.stringify(v).toLowerCase().indexOf(q) >= 0);
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
    // Usage counts a server under its name as tool names spell it: "claude.ai Todoist" is claude_ai_Todoist.
    function usageKey(pick) {
        return pick.kind === 'MCP server' ? 'MCP server: ' + pick.name.replace(/[^\w-]/g, '_') : pickKey(pick);
    }
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
            if (kind === 'tool') deny.push(name);
            else if (kind === 'subagent') deny.push('Agent(' + name + ')');
            else if (kind === 'skill') skills.push(name);
            else if (kind === 'MCP server') servers.push(name);
        });
        var count = picked().length;
        var L = ['Switch off ' + (count === 1 ? 'this part' : 'these ' + count + ' parts') +
                 ' of my Claude Code context by editing my user settings file: ~/.claude/settings.json, or ' +
                 'settings.json in $CLAUDE_CONFIG_DIR if that is set.', ''];
        if (deny.length) {
            L.push('Add these entries to "permissions" > "deny", and keep the entries already there:');
            deny.forEach(function (entry) { L.push('- ' + JSON.stringify(entry)); });
            L.push('');
        }
        if (skills.length) {
            // The entry depends on where the skill comes from, which only Claude can check on the machine.
            L.push('Switch off these skills: ' + skills.join(', ') + '. Use the entry that fits where each one comes from:');
            L.push('- A skill bundled with Claude Code, or one in ~/.claude/skills/ or a project\'s .claude/skills/: ' +
                   'set "skillOverrides": {"NAME": "off"}. This works for bundled skills too.');
            L.push('- A plugin\'s skill (listed as PLUGIN:NAME): skillOverrides does not reach it, it goes only with its ' +
                   'whole plugin. Tell me which plugin it is, and ask me before you set "PLUGIN@MARKETPLACE": false ' +
                   'in "enabledPlugins".');
            L.push('- A skill synced from claude.ai (under ~/.claude/skills/synced/): tell me to switch it off in my ' +
                   'claude.ai skill settings. "syncClaudeAiSkills": false would stop every synced skill, so ask me ' +
                   'before you set that.');
            L.push('The rules are in https://code.claude.com/docs/en/skills#override-skill-visibility-from-settings.');
            L.push('');
        }
        // deniedMcpServers keeps Claude Code from connecting to the server at all, so its tools and
        // instructions are gone; a "permissions" > "deny" rule would leave it connected. It works
        // from the user settings file wherever the server is defined, so Claude need not look for that.
        if (servers.length) {
            L.push('Disconnect these MCP servers: add these entries to "deniedMcpServers", and keep the entries ' +
                   'already there. This blocks a server wherever it is defined (~/.claude.json, a project\'s ' +
                   '.mcp.json, a plugin), so don\'t edit or remove its definition:');
            // A server that sent no instructions is known only as its tool names spell it.
            servers.forEach(function (name) {
                L.push('- ' + JSON.stringify({ serverName: name }) + ((data.servers || []).indexOf(name) < 0 ?
                       ' (as its tool names spell it)' : ''));
            });
            // An entry that is spelled differently blocks nothing, and nothing says so.
            L.push('An entry blocks only a server of exactly that name. Run `claude mcp list` first and use each ' +
                   'name as it shows it; a claude.ai connector is named like "claude.ai Todoist".');
            L.push('');
        }
        L.push('Before you write, show me the exact change and ask me. Leave everything else in the file as it is.');
        if (servers.length) L.push('After you write, run `claude mcp list` again and check that each blocked ' +
                                   'server is no longer listed.');
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

    // A shared part has no tokens or cache segment of its own: they depend on the request, so they
    // are worked out for the request on show, as tokens() and segments() do in profiler.py. Only
    // the answer's parts come with their tokens.
    function prepare(r) {
        var ratio = r.info.ratio || 4, segment = 0, added = {};
        (r.info.new || []).forEach(function (path) { added[path] = true; });
        r.newTok = 0;
        var tok = function (item) {
            if (item.fixed === undefined) item.fixed = item.tok === undefined ? null : item.tok;
            item.tok = item.fixed !== null ? item.fixed : Math.round(item.chars / ratio);
            if (added[item.path]) r.newTok += item.tok;
            item.children.forEach(tok);
        };
        var seg = function (item) {
            item.seg = segment;
            item.children.forEach(seg);
            if (item.bp) segment++;
        };
        tok(r);
        ['tools', 'system', 'messages'].forEach(function (path) {
            r.children.forEach(function (part) { if (part.path === path) seg(part); });
        });
    }

    // Where a part stands to what the request adds to the previous one in its conversation: one of
    // the new parts, inside one, or around one.
    function newness(path) {
        var list = root().info.new || [];
        for (var i = 0; i < list.length; i++) {
            if (path === list[i]) return 'new';
            if (path.indexOf(list[i] + '/') === 0) return 'inside';
            if (path === 'request' || list[i].indexOf(path + '/') === 0) return 'around';
        }
        return null;
    }

    function select(req, path) {
        state.req = req;
        prepare(root());
        state.path = resolve(path);
        var parts = state.path.split('/');
        for (var i = 1; i < parts.length; i++) state.open[parts.slice(0, i).join('/')] = true;
        renderPicker(); renderTree(); renderPane();
        var first = query() && $('pane').querySelector('mark');
        if (first) first.scrollIntoView({ block: 'center' });
        persist();
    }

    // The live viewer reloads the page when a call lands. Keep the selection
    // across that reload, and stay on the newest request if that is where the
    // reader was.
    var storeKey = 'profile:' + document.title;
    // The request is kept by its record's index, not its place in the list: a slow call that lands
    // late is sorted in before the calls that overtook it. The scroll positions and the search box's
    // focus are saved as the page unloads, so a live reload leaves the reader where they were.
    function persist() {
        try {
            sessionStorage.setItem(storeKey, JSON.stringify({
                req: state.req, index: root().info.index, atEnd: state.req === requests.length - 1, path: state.path,
                sortBySize: state.sortBySize, open: state.open, jsonOpen: state.jsonOpen, paneSort: state.paneSort,
                picked: state.picked, dialog: state.dialog, rawView: state.rawView, query: state.query, onlyNew: state.onlyNew, view: state.view, timelineOpen: state.timelineOpen,
                treeTop: document.querySelector('.tree').scrollTop, paneTop: $('pane').scrollTop,
                searching: document.activeElement === $('search')
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
    $('tab-request').onclick = function () { showView('request'); };
    $('tab-session').onclick = function () { showView('session'); };
    $('prompt-copy').onclick = function () {
        var b = $('prompt-copy');
        copyText($('prompt-text').textContent,
                 function () { b.textContent = 'Copied'; },
                 function () { b.textContent = 'Copy failed: select the text'; });
    };
    $('prompt-close').onclick = function () { $('prompt-dialog').close(); };
    $('prompt-dialog').addEventListener('close', function () { state.dialog = false; persist(); });
    $('clear').onclick = function () { state.picked = {}; renderTree(); renderPickBar(); persist(); };
    $('only-new').onclick = function () {
        state.onlyNew = !state.onlyNew;
        $('only-new').className = state.onlyNew ? 'on' : '';
        renderTree(); persist();
    };
    $('rows').setAttribute('role', 'tree');
    $('rows').setAttribute('aria-label', 'parts of the request');
    $('search').oninput = function () { setQuery($('search').value); };
    $('search').onkeydown = function (ev) {
        if (ev.key === 'Escape') { $('search').value = ''; setQuery(''); $('search').blur(); }
    };
    document.addEventListener('keydown', function (ev) {
        if ($('prompt-dialog').open) return;
        if (ev.target === $('search')) return;
        if (ev.key === '/' && state.view === 'request') { ev.preventDefault(); $('search').focus(); return; }
        if (ev.key === 'ArrowRight' && state.req < requests.length - 1) select(state.req + 1, state.path);
        if (ev.key === 'ArrowLeft' && state.req > 0) select(state.req - 1, state.path);
        if (ev.key === 'Home' && state.req > 0) { ev.preventDefault(); select(0, state.path); }
        if (ev.key === 'End' && state.req < requests.length - 1) { ev.preventDefault(); select(requests.length - 1, state.path); }
    });
    var saved = restore();
    if (saved) {
        if (saved.sortBySize !== undefined) state.sortBySize = !!saved.sortBySize;
        state.open = saved.open || {};
        // MCP tools are no longer ticked one by one; their servers are.
        Object.keys(saved.picked || {}).forEach(function (k) { if (k.indexOf('MCP tool: ')) state.picked[k] = true; });
        state.rawView = !!saved.rawView;
        state.query = $('search').value = saved.query || '';
        state.jsonOpen = saved.jsonOpen || {};
        state.onlyNew = !!saved.onlyNew;
        if (saved.view === 'session') state.view = 'session';
        state.timelineOpen = !!saved.timelineOpen;
        state.paneSort = saved.paneSort || null;
        var at = -1;
        requests.forEach(function (r, i) { if (r.info.index === saved.index) at = i; });
        if (at < 0) at = Math.min(saved.req || 0, requests.length - 1);
        select(saved.atEnd ? requests.length - 1 : at, saved.path || 'request');
        if (saved.treeTop) document.querySelector('.tree').scrollTop = saved.treeTop;
        if (saved.paneTop) $('pane').scrollTop = saved.paneTop;
        if (saved.searching) $('search').focus();
        // A live reload while the dialog is open brings it back.
        if (saved.dialog && picked().length) openPrompt();
    } else {
        select(0, 'request');
    }
    $('sort').className = state.sortBySize ? 'on' : '';
    $('only-new').className = state.onlyNew ? 'on' : '';
    renderPickBar();
    window.addEventListener('pagehide', persist);
})();
"""


def node_table(requests: list[dict]) -> tuple[list[dict], list[int]]:
    """Every node once, its children as indexes into the list, and each request's root index. A part
    shared by many requests is written once; the page links the indexes back into trees."""
    index: dict[int, int] = {}
    nodes: list[dict] = []

    def visit(item: dict) -> int:
        if id(item) not in index:
            out = {k: v for k, v in item.items() if k != "children"}
            index[id(item)] = len(nodes)
            nodes.append(out)
            out["children"] = [visit(child) for child in item["children"]]
        return index[id(item)]

    return nodes, [visit(request) for request in requests]


def where(profile: dict) -> str:
    """The folder and branch for the browser tab's title, first, so tabs of different sessions tell
    apart."""
    if not profile.get("project"):
        return ""
    name = Path(profile["project"]).name or profile["project"]
    return f"{name} ({profile['branch']}) \u00b7 " if profile.get("branch") else f"{name} \u00b7 "


def render_page(run_dir: Path, profile: dict) -> str:
    nodes, roots = node_table(profile["requests"])
    profile = dict(profile, nodes=nodes, requests=roots)
    # Every "<" is escaped, not only "</": a "<!--" followed by "<script" in recorded text would put
    # the browser's parser in a mode where the closing </script> no longer ends the data.
    payload = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(where(profile))}context profile {esc(run_dir.name)}</title>
<style>{CSS}</style></head>
<body>
<div class="picker" id="picker" title="&larr; &rarr; switch request, Home and End go to the first and last"></div>
<div class="charts" id="charts"></div>
<div class="split">
  <div class="tree">
    <div class="tabs" role="tablist">
      <button id="tab-request" role="tab" title="what the selected request is made of">Request</button>
      <button id="tab-session" role="tab" title="the session as Claude Code shows it">Session</button>
    </div>
    <div class="toolbar">
      <button id="sort">sort by size</button>
      <button id="expand">expand all</button>
      <button id="collapse">collapse</button>
      <button id="only-new" title="show only what this request adds to the previous one">only new</button>
      <button id="copy" title="show a prompt that asks Claude to switch the selected items off">Disable prompt</button>
      <button id="clear">clear</button>
      <input id="search" type="search" placeholder="search" title="keep the parts that contain this text (/)">
      <span id="found" class="found"></span>
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
    # The folder Claude ran in and its branch, as the launcher found them when the session started.
    try:
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    profile["project"] = meta.get("project") if isinstance(meta, dict) else None
    profile["branch"] = meta.get("branch") if isinstance(meta, dict) else None
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
