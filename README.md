# ccwrap

See exactly what fills Claude Code's context: every tool, skill, subagent, MCP server and message,
with its tokens and full text. Everything runs on your machine, and nothing is sent anywhere new.

![The ccwrap context profiler page](docs/screenshot.png)

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Claude Code on your PATH. In any project, run:

```bash
uvx --from git+ssh://git@github.com/MindWrapper/ccwrap ccwrap
```

Claude Code starts as usual. At your first message, a page opens in your browser and follows the
session live. Open a request's tree, click any part to read it, and sort by size to see what costs
the most. Tick items and press **copy selected** to copy them as `kind: name` lines.

- Anything after `ccwrap` goes to `claude` unchanged, such as `ccwrap --continue`.
- To install it: `uv tool install git+ssh://git@github.com/MindWrapper/ccwrap`. The repo is private
  for now; once it is public, this becomes `uvx ccwrap`.
- `CCWRAP_OPEN=0` keeps the browser closed. `CCWRAP_HOME` moves the recordings (default `~/.ccwrap`).

## Your data stays local

- ccwrap adds no server, account or telemetry. Claude's requests go to the same API as always.
- The recorder and the page listen on `127.0.0.1` only. The page loads nothing from the internet.
- Auth headers are redacted before anything touches the disk.
- Recordings hold your prompts and the files Claude read. They live in `~/.ccwrap/recordings/`;
  delete them any time.

## Switching things off

ccwrap changes nothing; that part is up to you. The usual entries in `~/.claude/settings.json`:

| To switch off                     | Add                                                         |
|-----------------------------------|-------------------------------------------------------------|
| a tool, e.g. `WebFetch`           | its name to `permissions.deny`                              |
| an MCP server or one of its tools | `mcp__SERVER` or `mcp__SERVER__TOOL` to `permissions.deny`  |
| a subagent type                   | `Agent(NAME)` to `permissions.deny`                         |
| a plugin                          | `"NAME@MARKETPLACE": false` in `enabledPlugins`             |
| a skill                           | `"NAME": "off"` in `skillOverrides`                         |

`permissions.deny` is the easiest to undo: remove an entry mid-session and the tool is back on the
next request, without breaking the prompt cache. For the Artifact tool, deny `Artifact` rather than
setting `enableArtifact: false`, which no other settings file can turn back on. See the
[settings docs](https://code.claude.com/docs/en/settings).

## Limits

- Bedrock, Vertex and Foundry ignore the base URL that recording relies on. ccwrap then runs Claude
  Code without recording, as it does whenever the recorder cannot start.
- The Claude desktop app is not recorded. A terminal session on the same config is close; the app
  adds a few tools of its own.
- Token counts per part are estimates. The total per request is the API's own number.
- Windows is supported, but tested by hand.

## Development

Run the tests with `python -m unittest discover -s tests`, or run from a checkout with
`uv run ccwrap`. See `AGENTS.md` for the design. MIT license.
