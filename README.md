# ccwrap

See what fills Claude Code's context before you type a word, and switch off what you don't use.

ccwrap starts one Claude Code session with a recorder in front of it. A page opens in your
browser and shows every part of each request to the model: system prompt, tools, MCP servers,
connectors, plugins, skills and subagents, each with its estimated tokens. Tick what you don't
need and the page gives you a prompt. Paste it into Claude, and Claude writes the changes into
your settings. After that you don't need ccwrap; your normal `claude` starts lean.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Claude Code on your PATH.

1. In any project directory, run:

   ```bash
   uvx --from git+ssh://git@github.com/MindWrapper/ccwrap ccwrap
   ```

   Claude Code starts as usual. Send any message. The page opens in your browser at the first
   request and follows the session live.

2. In the page, tick the items you don't need. The page shows what each one costs and the total
   you save.

3. Copy the prompt from the page and paste it into Claude. Claude adds the changes to
   `~/.claude/settings.json` and asks before it writes.

4. Quit and start Claude again. Run `/context` to see the smaller context, or run ccwrap again
   to see the new requests in full.

The repo is private for now, so step 1 needs SSH access to it. Once it is public, the command
becomes `uvx ccwrap`.

## Other ways to start it

Anything after `ccwrap` goes to `claude` unchanged:

```bash
uvx --from git+ssh://git@github.com/MindWrapper/ccwrap ccwrap --continue
```

To keep `ccwrap` on your PATH instead of fetching it each time:

```bash
uv tool install git+ssh://git@github.com/MindWrapper/ccwrap
```

Environment variables:

| Variable      | Meaning                                                                  |
|---------------|--------------------------------------------------------------------------|
| `CCWRAP_OPEN` | `0` keeps the browser closed; the terminal prints the page address       |
| `CCWRAP_PORT` | the first port to try: the recorder uses N, the page N+1 (default: free) |
| `CCWRAP_HOME` | where recordings are kept (default `~/.ccwrap`)                          |

For example:

```bash
CCWRAP_OPEN=0 uvx --from git+ssh://git@github.com/MindWrapper/ccwrap ccwrap
```

### The Claude desktop app

Record in a terminal, as above. The changes go into `~/.claude/settings.json`, which the Code
tab of the desktop app reads too, so they apply there as well. The desktop app adds a few tools
of its own (browser, session tools) that a terminal recording does not show.

## Getting a tool back

Every change is one entry in `~/.claude/settings.json`. To bring something back, remove its
entry yourself; Claude may decline to loosen its own permissions.

| You switched off                          | The entry                            | To bring it back                         |
|-------------------------------------------|--------------------------------------|------------------------------------------|
| a tool, MCP server, connector or subagent | a name in `permissions.deny`         | delete the name, or use `/permissions`   |
| a plugin                                  | `"NAME@MARKETPLACE": false` in `enabledPlugins` | set it to `true`, or use `/plugin` |
| a skill                                   | `"NAME": "off"` in `skillOverrides`  | delete the entry                         |
| the bundled skills                        | `"disableBundledSkills": true`       | delete the entry                         |
| all claude.ai connectors                  | `"disableClaudeAiConnectors": true`  | delete the entry                         |

A `permissions.deny` entry works mid-session: remove it and the tool is there on the next
request, with no restart. Claude Code appends the tool to the conversation, so you pay once for
its definition and the rest of the conversation stays cached. Put the entry back and the tool
is blocked again, but its definition stays in the conversation until `/compact` or a new
session. The other entries may need a new session.

So "off by default, on when I need it" is cheap. Switch off the Artifact tool, and when you want
to publish a page, delete `"Artifact"` from `permissions.deny` and ask for it.

## Privacy

A recording holds your prompts, the answers and the contents of files that Claude read. Treat it
like your transcripts. Auth headers are redacted before anything is written. Nothing leaves your
machine except the normal API traffic: the page is served on `127.0.0.1`. Recordings are kept
under `~/.ccwrap/recordings/`; delete them any time.

## Limits

- Recording points Claude Code at a local proxy through `ANTHROPIC_BASE_URL`. It does not work
  with Bedrock, Vertex or Foundry, which ignore the base URL; ccwrap then says so and runs Claude
  Code without recording. If the recorder cannot start for any other reason, the same happens:
  a broken recorder never costs you the session.
- Most MCP tools are deferred: only their names are in the context until Claude needs one. The
  page shows what is really in the request, so their savings are small, and that is accurate.
- The page cannot switch these off yet: project skills and commands, synced claude.ai skills, and
  a plugin that the project's own settings enable (project settings win over yours).
- The bundled skills saving is a maximum: some listed commands may stay.
- A plugin skill goes only with its whole plugin, so the page offers the plugin.
- Token counts are estimates. `/context` gives Claude Code's own numbers.
- Windows is supported, but tested by hand.

## Development

```bash
python -m unittest discover -s tests
```

Run from a checkout with `uv run ccwrap`. See `AGENTS.md` for the design.

## License

MIT.
