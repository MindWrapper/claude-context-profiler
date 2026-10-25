# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The version is `__version__` in
`src/claude_context_profiler/__init__.py`.

## [Unreleased]

- The timeline is laid out like a profiler's charts: one row per conversation, named on the left as
  a tree, with each subagent under the conversation that started it and side calls, such as the
  quota check, at the bottom. The selected request is lit down every row, and the requests linked to
  it are tinted: the one that started a subagent and the one that got its result back. An Agent call
  in the tree links to the subagent's first request, and the subagent's requests link back to it.

## [0.3.0]

- A toggled button, such as **sort by size** or **Only new**, now looks switched on.
- Each request shows what it adds to the previous one in its conversation: new parts have a blue
  dot, **only new** hides the rest, and the request's total says "+N new".
- The timeline's bars are split by the API's own counts: sent fresh and written to the cache on top,
  read from the cache below, so a bar shows what a request added and where the cache broke.
- Long sessions stay usable: each request shares the parts it has in common with the one before, so
  a 300-request page is about a tenth of the size and builds three times as fast. The live page
  reloads no more often than twice the time a load takes.
- The tree, the timeline and the links work from the keyboard: Tab reaches them, the arrow keys move
  through the tree and open or close parts, Enter selects and space ticks.
- A request that cuts an emoji in half no longer stops the recording for the rest of the session.
- The live page answers only its own address, so a website cannot read your recording through it.
- Recorded text can no longer leave the page blank.
- A Python file in your project, such as a `json.py`, no longer replaces the standard library in
  the recorder, the page or the start-screen hook.
- On Windows, a project folder with characters outside the system's code page no longer stops the
  recorder.
- An answer that breaks off midway is recorded as an error, and Claude Code sees the connection end
  instead of a garbled answer.
- A live reload keeps your place: the request, the open parts, the scroll positions and the search
  box.
- During a search, a matching part opens like any other part.
- An error while building the page no longer stops the live page from following the session.

## [0.2.0]

- **Disable prompt (N)** replaces **copy selected**: it opens a dialog with a prompt that asks Claude
  to switch the ticked items off in `~/.claude/settings.json`, showing the change and asking before
  it writes. The dialog shows the prompt in full, since Claude Code folds a pasted block into one line.
- The Disable prompt tells Claude which entry switches off a skill, by where it comes from:
  `skillOverrides` for a skill bundled with Claude Code or your own, the whole plugin for a plugin's
  skill, and claude.ai's settings for a synced one. Before, Claude did not know how to switch off a
  bundled skill.
- MCP tools are grouped under their server, which carries the only checkbox. The Disable prompt
  switches a ticked server off with a `deniedMcpServers` entry, so Claude Code no longer connects to
  it; the deny rules it used before left the server connected. A server that sends no instructions
  can now be ticked too.
- A search box keeps the parts of a request whose name or text contains the query, and highlights
  it in the tree and the pane; the timeline dims the requests without it. `/` jumps to it, Esc
  clears it.
- A tool, skill, subagent type or MCP server that the session used shows how often, "used 3×",
  counted over the whole recording; a deferred tool that was loaded but never called shows "loaded".
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
- Claude's start screen shows the page's address as a link. Only you see it; it stays out of the
  recorded context. The browser no longer opens by default; `CLAUDE_CONTEXT_PROFILER_OPEN=1` opens it
  at the first API call, as before.
- The launcher waits up to 30s for the recorder to start, not 10s: a fresh machine scans Python on
  its first start. When a session is not recorded, it says so again after Claude exits.
- The proxy and the page start without a reverse DNS lookup of 127.0.0.1, which hung for half a
  minute or more on some machines and kept the page from ever opening.
- The page drops its title, totals and footnote, and sorts each request by size from the start.
- The page shows tokens only, no character counts.
- The right pane shows each part's JSON as a readable tree: long strings as formatted text, Markdown
  rendered, a token estimate per key, and big objects folded. **View JSON** shows the exact JSON.

## [0.1.0]

First version.

- `ccprof` (also `claude-context-profiler`) records one Claude Code session through a local proxy
  and opens a page that follows it live.
- The page shows each request as a tree: every tool, skill, subagent type, MCP server and message,
  with its estimated tokens and full text.
- Tools, skills, subagent types and MCP servers can be ticked and copied as `kind: name` lines.
- Auth headers are blanked before a recording is written, and the page loads nothing from the
  internet.
