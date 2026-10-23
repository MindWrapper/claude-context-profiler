# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The version is `__version__` in
`src/claude_context_profiler/__init__.py`.

## [Unreleased]

- The Disable prompt tells Claude which entry switches off a skill, by where it comes from:
  `skillOverrides` for a skill bundled with Claude Code or your own, the whole plugin for a plugin's
  skill, and claude.ai's settings for a synced one. Claude used to stop at a bundled skill.
- MCP tools are grouped under their server, which carries the only checkbox: a server is switched
  off as a whole. One MCP tool costs a few tokens as a deferred name, and denying it left the server
  connected. A server that sends no instructions can now be ticked too.
- A search box keeps the parts of a request whose name or text contains the query, and highlights
  it in the tree and the pane; the timeline dims the requests without it. `/` jumps to it, Esc
  clears it.
- A tool, skill, subagent type or MCP server that the session used shows how often, "used 3×",
  counted over the whole recording; a deferred tool that was loaded but never called shows "loaded".
- The Disable prompt disconnects a ticked MCP server with a `deniedMcpServers` entry, so Claude Code
  no longer connects to it. The `mcp__SERVER` deny rule it used before left the server connected.
- A text that holds several listings is named after all of them ("listings: skills, MCP servers, …"),
  so the MCP servers no longer hide under "skills listing".
- A request's pane shows its tokens in `/context`'s categories (System prompt, System tools, MCP
  server instructions, Memory files, Skills, Messages); each row opens its part. The deferred tools
  listing is called "deferred tools (names only)": only the names are sent.
- MCP server instructions are split into one item per server again, so each can be read and ticked.
  Claude Code now puts a sentence before the servers, and a server's own text can have `##` headings.
- A request's pane shows Claude's session ID and the recording's folder, each with a copy button.
- A timeline replaces the row of request buttons: one bar per request, its height the prompt's
  tokens, so a long session stays one row. ‹ › and the arrow keys step through it.
- Token-counting calls (`/v1/messages/count_tokens`, which `/context` makes) are no longer listed as
  requests, and requests are numbered among themselves.
- The browser no longer opens by default: the page's address is on Claude's start screen.
  `CLAUDE_CONTEXT_PROFILER_OPEN=1` opens it at the first API call, as before.
- Claude's start screen shows the address of the live page in a box, as a link, so it can be found
  when the browser does not open. The message is shown to you only and stays out of the recorded
  context.
- The launcher waits up to 30s for the recorder to start, not 10s: a fresh machine scans Python on
  its first start. When a session is not recorded, it says so again after Claude exits.
- The proxy and the page start without a reverse DNS lookup of 127.0.0.1, which hung for half a
  minute or more on some machines and kept the page from ever opening.
- The page drops its title, totals and footnote, and sorts each request by size from the start.
- The page shows tokens only, no character counts.
- The right pane shows each part's JSON as a readable tree: long strings as formatted text, Markdown
  rendered, a token estimate per key, and big objects folded. **View JSON** shows the exact JSON.
- **Disable prompt (N)** replaces **copy selected**: it opens a dialog with a prompt that asks Claude
  to switch the ticked items off in `~/.claude/settings.json`, showing the change and asking before
  it writes. The dialog shows the prompt in full, since Claude Code folds a pasted block into one line.

## [0.1.0]

First version.

- `ccprof` (also `claude-context-profiler`) records one Claude Code session through a local proxy
  and opens a page that follows it live.
- The page shows each request as a tree: every tool, skill, subagent type, MCP server and message,
  with its estimated tokens and full text.
- Tools, skills, subagent types and MCP servers can be ticked and copied as `kind: name` lines.
- Auth headers are blanked before a recording is written, and the page loads nothing from the
  internet.
