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
a checkbox. The module is named `profiler.py` because `profile.py` would shadow the standard
library's `profile` module.

## Check a change here

Record a session (see `.claude/rules/recording.md`), then serve it with
`python3 -m claude_context_profiler.serve RUN_DIR --port 8791`, open it in a browser, and check the
console for errors. After a JavaScript edit, extract the page's last `<script>` and run
`node --check` on it. When the page's look changes, retake the README screenshot with the
`/update-screenshot` skill (`.claude/skills/update-screenshot/SKILL.md`).
