# ccwrap

See exactly what fills Claude Code's context. Everything runs on your machine, and nothing is
sent anywhere new.

ccwrap starts one Claude Code session with a recorder in front of it. A page opens in your
browser and shows every part of each request to the model: system prompt, tools, MCP servers,
connectors, plugins, skills, subagents and messages, each with its estimated tokens and its full
text. ccwrap only shows; it changes nothing. What you do about it is up to you.

## Your data stays on your machine

- **Nothing is sent anywhere new.** ccwrap adds no server, no account and no telemetry. Claude's
  requests go to the same API they always go to; ccwrap only keeps a copy on your disk.
- **Everything runs locally.** The recorder and the page listen on `127.0.0.1` only. The page is
  a single local file that loads nothing from the internet.
- **Credentials are never written.** Auth headers are redacted before anything touches the disk.
- **You own the recordings.** They hold your prompts, the answers and the files Claude read, so
  treat them like your transcripts. They live in `~/.ccwrap/recordings/`; delete them any time.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Claude Code on your PATH.

1. In any project directory, run:

   ```bash
   uvx --from git+ssh://git@github.com/MindWrapper/ccwrap ccwrap
   ```

   Claude Code starts as usual. Send any message. The page opens in your browser at the first
   request and follows the session live.

2. In the page, open the tree of a request to see what it is made of. Click any part to read
   its full text. Sort by size to find what costs the most.

3. Tick the tools, skills, subagent types and MCP servers you want to do something about, and
   press **copy selected**. The clipboard gets one line per item, such as `tool: WebFetch` or
   `skill: dataviz`, to paste wherever you like, for example into Claude with your own request.

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
| `CCWRAP_HOME` | where recordings are kept (default `~/.ccwrap`)                          |

For example:

```bash
CCWRAP_OPEN=0 uvx --from git+ssh://git@github.com/MindWrapper/ccwrap ccwrap
```

### The Claude desktop app

Record in a terminal, as above. A terminal session on the same config carries almost the same
context. The desktop app adds a few tools of its own (browser, session tools) that a terminal
recording does not show.

## If you want to switch something off

ccwrap leaves this to you. These are the usual entries in `~/.claude/settings.json`; see the
[Claude Code settings docs](https://code.claude.com/docs/en/settings) for the full story.

| To switch off                             | Add to `~/.claude/settings.json`                |
|-------------------------------------------|-------------------------------------------------|
| a built-in tool, e.g. `WebFetch`          | its name in `permissions.deny`                  |
| an MCP server, or one of its tools        | `mcp__SERVER` or `mcp__SERVER__TOOL` in `permissions.deny` |
| a subagent type                           | `Agent(NAME)` in `permissions.deny`             |
| a plugin                                  | `"NAME@MARKETPLACE": false` in `enabledPlugins` |
| a skill                                   | `"NAME": "off"` in `skillOverrides`             |
| the bundled skills                        | `"disableBundledSkills": true`                  |
| all claude.ai connectors                  | `"disableClaudeAiConnectors": true`             |

Prefer `permissions.deny` where it fits: it is the easiest to undo. Remove an entry mid-session
and the tool is there on the next request, with no restart. Claude Code appends the tool to the
conversation, so you pay once for its definition and the rest of the conversation stays cached.
Put the entry back and the tool is blocked again, but its definition stays in the conversation
until `/compact` or a new session. (Recorded with ccwrap on Claude Code 2.1.306.)

For the Artifact tool, deny `Artifact` rather than setting `"enableArtifact": false`: once any
settings file sets that to false, no other file can turn it back on.

## Limits

- Recording points Claude Code at a local proxy through `ANTHROPIC_BASE_URL`. It does not work
  with Bedrock, Vertex or Foundry, which ignore the base URL; ccwrap then says so and runs Claude
  Code without recording. If the recorder cannot start for any other reason, the same happens:
  a broken recorder never costs you the session.
- Most MCP tools are deferred: only their names are in the request until Claude needs one. The
  page shows what is really in the request, so they look small, and that is accurate.
- Token counts per part are estimates. The total per request is the API's own number.
- Windows is supported, but tested by hand.

## Development

```bash
python -m unittest discover -s tests
```

Run from a checkout with `uv run ccwrap`. See `AGENTS.md` for the design.

## License

MIT.
