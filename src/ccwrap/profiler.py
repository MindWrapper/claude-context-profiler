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
from datetime import datetime
from pathlib import Path

from .reader import (
    MESSAGES_PATH,
    block_text,
    build_exchanges,
    esc,
    is_messages_call,
    load_meta,
    load_run,
    render_totals,
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


def system_tree(blocks: list, store: Store) -> dict:
    root = node("system", "group", wire_size(blocks), ["system"], store)
    for i, block in enumerate(blocks):
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        path = ["system", str(i)]
        item = node(label_text(text), "text", wire_size(block), path, store, text=text, raw=block, bp=has_breakpoint(block))
        for name, body in split_sections(text):
            item["children"].append(node(name, "section", text_size(body), path + [name], store, text=body))
        root["children"].append(item)
    return root


def tools_tree(tools: list, store: Store) -> dict:
    root = node("tools", "group", wire_size(tools), ["tools"], store)
    for i, tool in enumerate(tools):
        name = tool.get("name") or tool.get("type") or f"tool {i}"
        child = node(name, "tool", wire_size(tool), ["tools", name], store, text=pretty(tool), bp=has_breakpoint(tool))
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


def message_node(i: int, message: dict, store: Store, tool_names: dict[str, str]) -> dict:
    role = message.get("role", "?")
    content = message.get("content")
    path = ["messages", str(i)]
    item = node("", role, wire_size(message), path, store, raw=message)
    if isinstance(content, str):
        item["text"] = store.put(content)
        item["label"] = f"{i} {role}: {label_text(content)}"
        item["bp"] = has_breakpoint(message)
        return item
    for j, block in enumerate(content or []):
        label = block_label(block, tool_names)
        kind = block.get("type", "?")
        child = node(label, kind, wire_size(block), path + [str(j)], store, text=block_text(block), raw=block, bp=has_breakpoint(block))
        item["children"].append(child)
    summary = ", ".join(child["label"] for child in item["children"]) or "(empty)"
    item["label"] = f"{i} {role}: {shorten(summary)}"
    return item


def messages_tree(messages: list, store: Store) -> dict:
    root = node("messages", "group", wire_size(messages), ["messages"], store)
    tool_names: dict[str, str] = {}
    for i, message in enumerate(messages):
        root["children"].append(message_node(i, message, store, tool_names))
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
            return ratio, "measured" if i == position else f"from request {exchanges[i]['record']['index']}"
    return FALLBACK_CHARS_PER_TOKEN, "fallback"


def request_tree(exchanges: list[dict], position: int, store: Store) -> dict:
    exchange = exchanges[position]
    body = exchange["body"]
    record = exchange["record"]
    root = node(f"request {record['index']}", "request", wire_size(body), ["request"], store)
    parts = [
        system_tree(system_blocks(body), store),
        tools_tree(body.get("tools") or [], store),
        messages_tree(body.get("messages") or [], store),
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
        --sel:#efe7fb; --warn:#b3261e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16161a; --fg:#e6e6e6; --muted:#9a9aa5; --line:#2c2c33; --card:#1d1d22;
          --code:#232329; --accent:#b18cf0; --bar:#4b3a73; --answer:#4ac97e; --sel:#2a2438; --warn:#ff7b72; }
}
* { box-sizing: border-box; }
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--fg); display:flex; flex-direction:column;
       font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { padding:1rem 1.5rem .5rem; border-bottom:1px solid var(--line); }
h1 { font-size:1.3rem; margin:0; }
.sub { color:var(--muted); margin:.2rem 0 0; font-size:13px; }
.picker { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; padding:.6rem 1.5rem; border-bottom:1px solid var(--line); }
.picker .chain { color:var(--muted); font-size:12px; margin-left:.6rem; }
.picker button { font:12px ui-monospace, monospace; padding:.2rem .5rem; }
.picker button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
button { font:inherit; padding:.3rem .7rem; border:1px solid var(--line); border-radius:6px;
         background:var(--card); color:var(--fg); cursor:pointer; }
button:hover { background:var(--code); }
button.on { background:var(--code); }
.split { flex:1; display:grid; grid-template-columns:minmax(360px, 44%) 1fr; min-height:0; }
.tree, .pane { overflow:auto; min-height:0; }
.tree { border-right:1px solid var(--line); }
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
.pane { padding:1rem 1.5rem; }
.pane h2 { font-size:1rem; margin:0 0 .3rem; word-break:break-word; }
.crumbs { color:var(--muted); font:12px ui-monospace, monospace; margin-bottom:.3rem; }
.crumbs a { color:var(--accent); cursor:pointer; }
.facts { display:flex; flex-wrap:wrap; gap:.3rem .9rem; font-size:13px; color:var(--muted); margin-bottom:.8rem; }
.facts b { color:var(--fg); font-weight:500; font-variant-numeric:tabular-nums; }
.warn { color:var(--warn); }
pre { margin:.4rem 0; padding:.6rem .7rem; background:var(--code); border-radius:6px; overflow:auto;
      white-space:pre-wrap; word-break:break-word; font:12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; font-size:13px; margin:.5rem 0 1rem; }
th, td { padding:.3rem .5rem; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
th:first-child, td:first-child { text-align:left; white-space:normal; }
th { color:var(--muted); font-weight:600; cursor:pointer; user-select:none; }
th:hover, th.on { color:var(--fg); }
td a { color:var(--accent); cursor:pointer; }
.note { color:var(--muted); font-size:12px; margin:.5rem 0; }
footer { padding:.5rem 1.5rem; border-top:1px solid var(--line); color:var(--muted); font-size:12px; }
"""

JS = r"""
(function () {
    var data = JSON.parse(document.getElementById('data').textContent);
    var requests = data.requests, content = data.content;
    // paneSort is null while the table follows the tree's sort toggle; clicking
    // a column header gives the table an order of its own.
    var state = { req: 0, path: 'request', sortBySize: false, raw: false, open: {}, paneSort: null };
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

    function renderPicker() {
        var box = $('picker');
        box.innerHTML = '';
        var chain = null;
        requests.forEach(function (r, i) {
            if (r.info.chain !== chain) {
                chain = r.info.chain;
                box.appendChild(el('span', 'chain', 'chain ' + chain));
            }
            var b = el('button', i === state.req ? 'on' : '', '#' + r.info.index + ' ' + n(r.info.prompt_in));
            b.title = r.info.model + ' · ' + n(r.info.prompt_in) + ' in · ' + n(r.info.output) + ' out';
            b.onclick = function () { select(i, state.path); };
            box.appendChild(b);
        });
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
            lbl.appendChild(el('span', 'name', item.label));
            if (item.bp) lbl.appendChild(el('span', 'bp', 'cache'));
            if (item.seg !== undefined && item.seg !== parentSeg) lbl.appendChild(el('span', 'seg', 'seg ' + (item.seg + 1)));
            row.appendChild(lbl);
            var bar = el('div', 'bar'); var fill = el('i');
            fill.style.width = (total ? Math.max(0.5, 100 * item.tok / total) : 0) + '%';
            bar.appendChild(fill); row.appendChild(bar);
            var num = el('div', 'num'); num.innerHTML = '<b>' + n(item.tok) + '</b>';
            num.title = n(item.chars) + ' chars';
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
            fact(facts, 'chars', n(item.chars));
            fact(facts, 'chars/token', info.ratio + ' (' + info.ratio_source + ')');
            fact(facts, 'cache segments', info.segments);
            fact(facts, 'took', n(info.duration_ms) + ' ms');
            if (info.stop_reason) fact(facts, 'stop', info.stop_reason);
            if (info.aborted) fact(facts, '', 'aborted', 'warn');
            if (info.status >= 400) fact(facts, 'HTTP', info.status, 'warn');
        } else {
            fact(facts, 'kind', item.kind);
            fact(facts, 'chars', n(item.chars));
            fact(facts, '≈ tokens', n(item.tok));
            fact(facts, inAnswer ? 'of output' : 'of prompt', pct(item.tok, total));
            if (item.hidden_tok) fact(facts, 'not visible', n(item.hidden_tok) + ' tokens');
            if (item.seg !== undefined) fact(facts, 'cache segment', (item.seg + 1) + ' of ' + info.segments);
            if (item.bp) fact(facts, '', 'breakpoint ends here', 'warn');
        }
        pane.appendChild(facts);

        if (item.children.length) {
            var t = el('table');
            var head = el('tr');
            [['part', 'wire'], ['chars', 'size'], ['≈ tokens', 'size'], ['share', 'size']].forEach(function (col) {
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
                tr.appendChild(el('td', '', n(k.chars)));
                tr.appendChild(el('td', '', n(k.tok)));
                tr.appendChild(el('td', '', pct(k.tok, k.kind === 'answer' ? k.tok : (item.kind === 'answer' ? item.tok : total))));
                t.appendChild(tr);
            });
            pane.appendChild(t);
        }
        if (item.raw && item.text) {
            var b = el('button', state.raw ? 'on' : '', state.raw ? 'show text' : 'show raw JSON');
            b.onclick = function () { state.raw = !state.raw; renderPane(); };
            pane.appendChild(b);
        }
        var key = state.raw && item.raw ? item.raw : (item.text || item.raw);
        if (key) pane.appendChild(el('pre', '', content[key]));
        else if (!item.children.length) pane.appendChild(el('p', 'note', 'No content in the recording.'));
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
                sortBySize: state.sortBySize, open: state.open
            }));
        } catch (e) { /* no storage: the page still works, it just forgets */ }
    }
    function restore() {
        try { return JSON.parse(sessionStorage.getItem(storeKey)); } catch (e) { return null; }
    }

    $('sort').onclick = function () { state.sortBySize = !state.sortBySize; state.paneSort = null; $('sort').className = state.sortBySize ? 'on' : ''; renderTree(); renderPane(); persist(); };
    $('expand').onclick = function () { var all = function (it) { state.open[it.path] = true; it.children.forEach(all); }; all(root()); renderTree(); persist(); };
    $('collapse').onclick = function () { state.open = {}; renderTree(); persist(); };
    document.addEventListener('keydown', function (ev) {
        if (ev.target.tagName === 'INPUT') return;
        if (ev.key === 'ArrowRight' && state.req < requests.length - 1) select(state.req + 1, state.path);
        if (ev.key === 'ArrowLeft' && state.req > 0) select(state.req - 1, state.path);
    });
    var saved = restore();
    if (saved) {
        state.sortBySize = !!saved.sortBySize;
        $('sort').className = state.sortBySize ? 'on' : '';
        state.open = saved.open || {};
        select(saved.atEnd ? requests.length - 1 : Math.min(saved.req || 0, requests.length - 1), saved.path || 'request');
    } else {
        select(0, 'request');
    }
})();
"""


def render_page(run_dir: Path, exchanges: list[dict], profile: dict, meta: dict) -> str:
    started = meta.get("started", "?")
    payload = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>context profile {esc(run_dir.name)}</title>
<style>{CSS}</style></head>
<body>
<header>
<h1>What the prompt is made of</h1>
<p class="sub">{esc(run_dir)} &middot; started {esc(started)} &middot;
rendered {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
{render_totals(exchanges)}
</header>
<div class="picker" id="picker"></div>
<div class="split">
  <div class="tree">
    <div class="toolbar">
      <span>&larr; &rarr; switch request</span>
      <button id="sort">sort by size</button>
      <button id="expand">expand all</button>
      <button id="collapse">collapse</button>
    </div>
    <div id="rows"></div>
  </div>
  <div class="pane" id="pane"></div>
</div>
<footer>Token counts per part are estimates: the request's characters divided by its real
<code>input_tokens</code> gives a chars-per-token ratio, applied to every part, so the parts add up to
the real total. The answer's visible parts use the same ratio; what the API's <code>output_tokens</code>
counts beyond them is thinking the recording cannot see (Claude Code asks the API to omit the thinking
text) and is put on the thinking node. Cache segments are numbered in the order the API hashes the
prompt: tools, then system, then messages.</footer>
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
    return render_page(run_dir.resolve(), exchanges, build_profile(exchanges), load_meta(run_dir))


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
