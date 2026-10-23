# claude-context-profiler

Claude Context Profiler is a local tool designed to help you inspect and manage the context payload sent by Claude Code.  So you can optimize your workflow, save money and keep your model smarter.

![The context profiler page](docs/screenshot.png)

**Example:** switching off two unused tools freed up 14,494 tokens in the context.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Claude Code on your PATH.

1. Install the tool:

   ```bash
   uv tool install git+ssh://git@github.com/MindWrapper/claude-context-profiler
   ```

2. Go to the folder where you would normally start Claude Code, and type this where you would
   type `claude`. It starts Claude Code for you, with recording on:

   ```bash
   ccprof
   ```

   `ccprof` is the short name; `claude-context-profiler` does the same.

As Claude Code starts, its start screen shows the address of a page that follows the session live.
Click it to open the page in your browser. Open a request's tree, click any part to read it, sort by size to see what costs the most, and search for a word to find every part that contains it. An item the session used says how often ("used 3×"). To switch something off, tick it and
press **Disable prompt**. A dialog shows the prompt; press **Copy** and paste it into
Claude, which shows you the change to `~/.claude/settings.json` and asks before it writes. A
request's own pane shows Claude's session ID and the recording's folder, each with a copy button.
When you quit Claude, the recording stops and the page is saved.

## Your data stays local

- The profiler adds no server, account or telemetry. Claude's requests go to the same API as always.
- The recorder and the page listen on `127.0.0.1` only. The page loads nothing from the internet.
- Your API key and sign-in token are never saved; the recording blanks them out. Extra headers you
  add with `ANTHROPIC_CUSTOM_HEADERS` are saved as they are.
- Recordings hold your prompts and the files Claude read. They live in
  `~/.claude-context-profiler/recordings/`; delete them any time.

## Switching things off

The profiler never changes your config itself; the **Disable prompt** button hands that to
Claude, with your approval. These are the entries it uses, if you prefer to edit
`~/.claude/settings.json` by hand:

| To switch off                     | Add                                                         |
|-----------------------------------|-------------------------------------------------------------|
| a tool, e.g. `WebFetch`           | its name to `permissions.deny`                              |
| an MCP server                     | `{"serverName": "NAME"}` to `deniedMcpServers`              |
| a subagent type                   | `Agent(NAME)` to `permissions.deny`                         |
| a plugin                          | `"NAME@MARKETPLACE": false` in `enabledPlugins`             |
| a skill, bundled or your own      | `"NAME": "off"` in `skillOverrides`                         |
| a plugin's skill                  | its whole plugin, as above                                  |
| a skill synced from claude.ai     | switch it off in claude.ai's skill settings                 |

`permissions.deny` is the easiest to undo: remove an entry mid-session and the tool is back on the
next request, without breaking the prompt cache. For the Artifact tool, deny `Artifact` rather than
setting `enableArtifact: false`, which no other settings file can turn back on. A server in
`deniedMcpServers` is disconnected: Claude Code no longer connects to it, in any project, so neither
its tools nor its instructions reach the context. The page ticks an MCP server only as a whole:
one of its tools costs a few tokens, as Claude Code sends only its name until Claude loads it. See the
[settings docs](https://code.claude.com/docs/en/settings).

## Options and updates

- Options are forwarded to `claude`: `ccprof --continue` runs `claude --continue`
  with recording on.
- To try it without installing, `uvx` downloads and runs it in one go:
  `uvx --from git+ssh://git@github.com/MindWrapper/claude-context-profiler ccprof`.
- To update: `uv tool upgrade claude-context-profiler`. To remove:
  `uv tool uninstall claude-context-profiler`.
- `CLAUDE_CONTEXT_PROFILER_OPEN=1` also opens the page in your browser at the first API call.
  `CLAUDE_CONTEXT_PROFILER_HOME` moves the recordings (default `~/.claude-context-profiler`).
- The repo is private for now, so installing needs SSH access to it.

## Without uv

The profiler is plain Python with no dependencies, so a clone and Python 3.9 or later are enough:

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

## Limits

- Bedrock, Vertex and Foundry ignore the base URL that recording relies on. The profiler then runs
  Claude Code without recording, as it does whenever the recorder cannot start.
- The Claude desktop app cannot be recorded: it sets `ANTHROPIC_BASE_URL` itself and does not let
  a session override it. Record a terminal session on the same config instead; it is close, the app
  only adds a few tools of its own.
- Token counts per part are estimates. The total per request is the API's own number.

## Development

Run the tests with `python -m unittest discover -s tests`, or run from a checkout with
`uv run ccprof`. See `AGENTS.md` for the design. MIT license.
