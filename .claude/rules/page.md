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
