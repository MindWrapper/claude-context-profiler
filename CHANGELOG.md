# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The version is `__version__` in
`src/claude_context_profiler/__init__.py`.

## [Unreleased]

## [0.1.0]

First version.

- `ccprof` (also `claude-context-profiler`) records one Claude Code session through a local proxy
  and opens a page that follows it live.
- The page shows each request as a tree: every tool, skill, subagent type, MCP server and message,
  with its estimated tokens and full text.
- Tools, skills, subagent types and MCP servers can be ticked and copied as `kind: name` lines.
- Auth headers are blanked before a recording is written, and the page loads nothing from the
  internet.
