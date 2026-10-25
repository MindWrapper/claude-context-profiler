# claude-context-profiler

See exactly what Claude Code sends to the model, request by request, live as you work: every tool,
skill, MCP server and message, with its tokens and its full text.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Claude Code on your PATH.

1. Install it:

   ```bash
   uv tool install git+https://github.com/MindWrapper/claude-context-profiler
   ```

2. In your project folder, start Claude Code with `ccprof` instead of `claude`:

   ```bash
   ccprof
   ```

3. Claude's start screen shows the address of your live page. Click it:

   ```
   ╭────────────────────────────────────────────────╮
   │  Context profiler  →  http://127.0.0.1:54972/  │
   ╰────────────────────────────────────────────────╯
   ```

4. Work with Claude as usual, and watch your context build up, request by request:

   ![The context profiler page](docs/screenshot.png)

   - **What a request is made of:** tools, system prompt, skills, MCP servers and messages, biggest
     first. Click any part to read it, or search for a word.
   - **What each request adds:** new parts have a blue dot, and **Only new** hides the rest.
   - **What the cache did:** each bar in the timeline shows what was read from the cache, written to
     it, or sent fresh.
   - **Where the subagents ran:** each subagent gets its own row in the timeline, under the
     conversation that started it.
   - **What the session used:** "used 3×" next to the tools, skills and servers Claude called.

When you quit Claude, the recording stops and the page is saved; `ccprof` prints its path as it
exits.

## Your data stays local

- The profiler adds no remote server, account or telemetry. Claude's requests go to the same API
  as always.
- The recorder and the page listen on `127.0.0.1` only. The page loads nothing from the internet.
- Your API key and sign-in token are never saved; the recording blanks them out. Extra headers you
  add with `ANTHROPIC_CUSTOM_HEADERS` are saved as they are.
- Recordings hold your prompts and the files Claude read. They live in
  `~/.claude-context-profiler/recordings/`; delete them any time.

## Switching things off

Tick what you want off and press **Disable prompt**. It gives you a prompt to paste into Claude,
which shows you the change to `~/.claude/settings.json` and asks before it writes. The profiler
never changes your config itself. To bring an item back, undo that change.

## Options and updates

- Options are forwarded to `claude`: `ccprof --continue` runs `claude --continue` with recording on.
- To update: `uv tool upgrade claude-context-profiler`. To remove:
  `uv tool uninstall claude-context-profiler`.
- `CLAUDE_CONTEXT_PROFILER_OPEN=1` also opens the page in your browser at the first API call.
  `CLAUDE_CONTEXT_PROFILER_HOME` moves the recordings (default `~/.claude-context-profiler`).
- Without uv: clone the repo and run `PYTHONPATH=CLONE/src python3 -m claude_context_profiler`;
  Python 3.9 or later is all it needs.

## Limits

- Bedrock, Vertex and Foundry ignore the base URL that recording relies on. The profiler then runs
  Claude Code without recording, as it does whenever the recorder cannot start.
- The recorder connects to the API directly, with Python's own certificates. Behind a corporate
  proxy (`HTTPS_PROXY`) or a network that needs extra certificates (`NODE_EXTRA_CA_CERTS`), Claude
  Code may reach the API while the recorder cannot, and the recorded session's requests fail. Run
  `claude` itself there.
- The Claude desktop app cannot be recorded: it sets `ANTHROPIC_BASE_URL` itself and does not let
  a session override it. Record a terminal session on the same config instead. It comes close: the
  app adds only a few tools of its own.
- The total for each request comes from the API, while the numbers for the parts are the profiler's
  estimates.
