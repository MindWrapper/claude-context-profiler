# claude-context-profiler

See exactly what fills Claude Code's context: every tool, skill, subagent, MCP server and message,
with its tokens and full text. Everything runs on your machine, and nothing is sent anywhere new.

![The context profiler page](docs/screenshot.png)

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Claude Code on your PATH. In any project, run:

```bash
uvx --from git+ssh://git@github.com/MindWrapper/claude-context-profiler claude-context-profiler
```

Claude Code starts as usual. At your first message, a page opens in your browser and follows the
session live. Open a request's tree, click any part to read it, and sort by size to see what costs
the most. Tick items and press **copy selected** to copy them as `kind: name` lines.

- Anything after the command goes to `claude` unchanged, such as `--continue`.
- To install it: `uv tool install git+ssh://git@github.com/MindWrapper/claude-context-profiler`.
  The repo is private for now; once it is public, the command becomes
  `uvx claude-context-profiler`.
- `CLAUDE_CONTEXT_PROFILER_OPEN=0` keeps the browser closed. `CLAUDE_CONTEXT_PROFILER_HOME` moves
  the recordings (default `~/.claude-context-profiler`).

## Without uv

The profiler is plain Python with no dependencies, so a clone and Python 3.10 or later are enough:

```bash
git clone https://github.com/MindWrapper/claude-context-profiler.git
```

Then, in your project, run it with the clone's `src` on the Python path:

```bash
PYTHONPATH=/path/to/claude-context-profiler/src python3 -m claude_context_profiler
```

On Windows, in PowerShell: `$env:PYTHONPATH = "C:\path\to\claude-context-profiler\src"`, then
`python -m claude_context_profiler`.

### Running the proxy yourself

The command above starts a proxy and the page, and points one `claude` at the proxy through
`ANTHROPIC_BASE_URL`. You can do the same by hand, each step in its own terminal with
`PYTHONPATH` set as above. Start the proxy first; it creates the recording folder:

```bash
python3 -m claude_context_profiler.proxy ~/context-profile --port 8787
```

```bash
python3 -m claude_context_profiler.serve ~/context-profile --port 8788 --open
```

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
```

When you are done, quit `claude` and press Ctrl-C in the other two terminals. The page is written
to `~/context-profile/context_profiler.html`. Use a new folder for each session; sessions that share
a folder are mixed together.

## Your data stays local

- The profiler adds no server, account or telemetry. Claude's requests go to the same API as always.
- The recorder and the page listen on `127.0.0.1` only. The page loads nothing from the internet.
- Auth headers are redacted before anything touches the disk.
- Recordings hold your prompts and the files Claude read. They live in
  `~/.claude-context-profiler/recordings/`; delete them any time.

## Switching things off

The profiler changes nothing; that part is up to you. The usual entries in `~/.claude/settings.json`:

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

- Bedrock, Vertex and Foundry ignore the base URL that recording relies on. The profiler then runs
  Claude Code without recording, as it does whenever the recorder cannot start.
- The Claude desktop app cannot be recorded: it sets `ANTHROPIC_BASE_URL` itself and does not let
  a session override it. Record a terminal session on the same config instead; it is close, the app
  only adds a few tools of its own.
- Token counts per part are estimates. The total per request is the API's own number.
- Windows is supported, but tested by hand.

## Development

Run the tests with `python -m unittest discover -s tests`, or run from a checkout with
`uv run claude-context-profiler`. See `AGENTS.md` for the design. MIT license.
