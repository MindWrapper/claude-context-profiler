---
paths:
  - "src/claude_context_profiler/profiler.py"
  - "src/claude_context_profiler/serve.py"
---

# The page: HTML, CSS and JavaScript

The profiler page is one self-contained HTML file. Its CSS and JavaScript are strings in
`profiler.py` (`CSS`, `JS`); the waiting page is in `serve.py` (`LIVE_JS`). The base is MDN's
guidance and OWASP's [XSS Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html).
This project adds:

- **Self-contained.** Inline every style and script; the page loads nothing from the network. The
  README promises this, and the final page is opened straight from disk.
- **Recorded text is untrusted**: prompts, tool output, web pages Claude read. Put it into the DOM
  with `textContent`, which the `el(tag, cls, text)` helper does. Use `innerHTML` only to clear an
  element or for markup built from numbers.
- **Data arrives as JSON** in `<script id="data" type="application/json">`, with every `<` escaped
  as `\u003c`, so recorded text can neither close the script tag nor change how the browser parses
  it. Pass new data the same way.
- **JavaScript matches the existing style:** ES5 (`var`, `function`) inside the one IIFE, no build
  step, no libraries. The script is a Python raw string (`r"""…"""`), so write it exactly as the
  browser should see it.
- **Browser storage is optional.** Wrap every `sessionStorage` access in `try`/`catch`; the page
  must work without it (pages opened from disk, private windows).
- **Clipboard:** `navigator.clipboard` needs a secure context, so keep the `execCommand('copy')`
  fallback for pages opened from disk.
- **Colors are custom properties** on `:root` (`--bg`, `--fg`, `--muted`, …), redefined under
  `prefers-color-scheme: dark`. A new color gets a property in both places.

## How the tree is built

`profiler.py` builds a tree of each request with estimated tokens per node. The listings Claude Code
puts into the prompt (skills, subagent types, deferred tools, MCP server instructions) are split into
one node per item by `LISTINGS`; each tool and listed item carries a `pick`, which the page shows as
a checkbox. In the MCP server instructions, a `## ` heading starts a server only when it names one
of the request's `mcp__SERVER__` tools (`server_key`), because a server's own text can have `## `
headings too; a listing ends at the next listing's header line. `group_by_server` gathers MCP tools, in `tools` and
in the tool listings, under one node per server that carries the server's checkbox; the tools carry
none. A server is named as its instructions' heading names it (`server_names`, session-wide), or by its
key when it sends none, and the prompt then asks Claude to look the name up. A text with several listings is
named after all of them, so none hides behind another's name.

Every request replays the conversation, so a part equal to the previous request's in the same chain
is that request's node, shared (`request_tree`, `messages_tree`). A shared node has no tokens or
cache segment of its own: `tokens()` and `segments()` work them out per request in Python, and
`prepare()` does the same in the page for the request on show; only the answer's nodes carry `tok`.
`node_table` writes each node once, its children as indexes, and the page links them back. `session_usage` counts how often
the session used each item with a `pick` (a call counts once by its id, since every request replays
the conversation), and the page shows it next to the item. The module is named `profiler.py`
because `profile.py` would shadow the standard library's `profile` module.

## The timeline

The top of the page is laid out like a profiler's charts: a toolbar that steps through the requests
with a legend of the bar colors, then one row per chain (`chain_links`), named in a column on the
left as a tree: the main conversation on top, each subagent under the chain that started it, side
calls (a chain without tools) at the bottom. A subagent is matched to its Agent call by the prompt,
which its first message holds. Each request is one bar, its height the prompt's tokens on one scale
for all rows, so the rows show how each context grew; a row is as tall as its tallest bar, and the
selected request is lit down every row. The requests linked to it are tinted: for a subagent's
request, the request that started the subagent and the one whose new messages carry its result; for
a request that started subagents, their first requests and those results. The Agent call's nodes
carry the call id (`call`), so the tree and the pane link the two ways. A gap starts a new main conversation,
a failed call is red, and the selected bar is scrolled into view. Each bar is split by the API's own counts:
fresh and cache writes on top, cache reads below; the request pane's facts carry the same colors as
a legend.

A request's `info.new` lists the paths of the parts it adds to the previous request in its chain
(`new_parts`): the messages after the shared ones (`kept`), and tools and system blocks that were
not there. A moved `cache_control` does not make a part new. The tree marks them with a blue
dot, and **only new** keeps them and the parts around them (`newness`). Requests are numbered among the requests:
`reader.is_messages_call` takes only the exact `/v1/messages` path, so the `count_tokens` calls
Claude Code makes (for `/context`, say) are not requests.

## The session tab

The left panel has two tabs: **Parts**, the tree of the request on show, and **Session**, the
session as Claude Code shows it (`session_steps`): what the user typed (`prompt_text`, the last
text block outside Claude Code's tags), Claude's text, and its tool calls as `Name(main argument)`
(`call_label`) with the first line of their results. A subagent's steps go under the Agent call
that started it. Each step names its request and the path of its part there; clicking it selects
that part, and the steps of the request on show are marked. The main conversation's requests that
Claude Code sends on the side (`side_questions`: a suggested next prompt, a recap) are left out: the
next request has another message where their question was, or, for the last request of a chain,
it started within a second of an answer that ended its turn and called no tools. A recap that ends
a chain still shows; nothing in it tells it from a prompt the user typed.

## Search

The search box keeps the parts whose label or text (`text` or `raw` in `content`) contains the
query, with the parts around them opened and dimmed. A part is a hit only when no part inside it is,
so a message is not counted again for its block. The timeline dims the requests without a hit, and
`highlight` wraps each occurrence in the tree and the pane in `<mark>`, splitting text nodes, so
recorded text still goes in as `textContent`.

## The keyboard

One tree row and one timeline bar take Tab: the selected ones. In the tree, the arrow keys move the
selection and open or close parts (`rowKey`), Enter selects and space ticks; on the timeline, the
arrow keys step through the requests. A link that selects a part (`link`) takes Tab and Enter.

## The right pane

A request shows its tokens in the categories `/context` uses (`categories` in `profiler.py`), so a
user can line the two up; the tree already shows the request's own parts. Messages is what the other
categories leave of the messages, as in `/context`. `/context` itself shows System tools less the
skills and adds them to Messages; the page does not copy that.

A part with JSON behind it (a tool, a message or system block, the envelope) shows that JSON as a
readable tree, `jsonView`: every key in its order, dim keys, long strings as formatted text, and a
token estimate per object and long string. It measures characters the way Python's `json.dumps`
writes them, so its numbers match the tree on the left. Plain text parts go through
`readableText`, a light Markdown renderer (headings, lists, code fences, `code`, **bold**). Both
build every node with `textContent`. **View JSON** (or **View raw** for plain text) switches to the
exact JSON or text, and **View formatted** switches back.

## Check a change here

Record a session (see `.claude/rules/recording.md`), then serve it with
`python3 -m claude_context_profiler.serve RUN_DIR --port 8791`, open it in a browser, and check the
console for errors. After a JavaScript edit, extract the page's last `<script>` and run
`node --check` on it. When the page's look changes, retake the README screenshot with the
`/update-screenshot` skill (`.claude/skills/update-screenshot/SKILL.md`).
