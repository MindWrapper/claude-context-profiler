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
- **Data arrives as JSON** in `<script id="data" type="application/json">`, with `</` escaped as
  `<\/` so recorded text cannot close the script tag. Pass new data the same way.
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
named after all of them, so none hides behind another's name. `session_usage` counts how often
the session used each item with a `pick` (a call counts once by its id, since every request replays
the conversation), and the page shows it next to the item. The module is named `profiler.py`
because `profile.py` would shadow the standard library's `profile` module.

## The timeline

The strip at the top has one bar per request, its height the prompt's tokens, so a session of any
length stays one row and shows how the context grew. A gap starts a new conversation chain, a failed
call is red, and the selected bar is scrolled into view. Requests are numbered among the requests:
`reader.is_messages_call` takes only the exact `/v1/messages` path, so the `count_tokens` calls
Claude Code makes (for `/context`, say) are not requests.

## Search

The search box keeps the parts whose label or text (`text` or `raw` in `content`) contains the
query, with the parts around them opened and dimmed. A part is a hit only when no part inside it is,
so a message is not counted again for its block. The timeline dims the requests without a hit, and
`highlight` wraps each occurrence in the tree and the pane in `<mark>`, splitting text nodes, so
recorded text still goes in as `textContent`.

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
