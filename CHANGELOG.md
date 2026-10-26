# Changelog

Versions follow [Semantic Versioning](https://semver.org/); the version is `__version__` in
`src/claude_context_profiler/__init__.py`.

## [Unreleased]

- **Session** tab: the session as Claude Code shows it, each subagent's steps under its Agent call.
- The timeline has one row per conversation: subagents under the one that started them, side calls
  at the bottom. It starts folded.
- The page shows the session's folder and git branch.
- « » and Home/End jump to the first and the last request.

## [0.3.0]

First version.

- `ccprof` records a Claude Code session through a local proxy and shows a live page of every
  request: each tool, skill, subagent type, MCP server and message, with its tokens and full text.
- Each request shows what it adds to the one before, and its bar on the timeline what it read from
  the cache, wrote to it, or sent fresh.
- Search, keyboard support, and "used 3×" next to what the session used.
- **Disable prompt**: tick tools, skills, subagent types and MCP servers, and get a prompt that has
  Claude switch them off in `~/.claude/settings.json`.
- Everything stays on your machine: API keys are blanked, and the page loads nothing from the
  internet.
