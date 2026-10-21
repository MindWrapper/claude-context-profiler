# ccwrap

`ccwrap` records one Claude Code session to show what every request to the model is made of, and
helps the user switch off what they do not need. The user runs it once, not every day:

1. `ccwrap` records a session on the user's normal config. The context profiler page opens in the
   browser and follows the session live.
2. In the page, the user ticks the tools, MCP servers, connectors, plugins, skills and subagents
   they do not need, and sees the tokens each one costs.
3. The page shows a prompt. The user pastes it into Claude, and Claude merges the changes into
   `~/.claude/settings.json`.
4. From then on the user's plain `claude` starts lean. `ccwrap` again shows the smaller context.

`README.md` is the user-facing spec. Read `docs/common-guidelines-code.md` before you write code. It
applies to all code here.

This is a bare first version. It covers the main case only; the owner grows it step by step.

## Command line

```
ccwrap [claude args...]   claude, recorded, with the profiler page live in the browser
```

- Every argument goes to `claude` unchanged. ccwrap has no options of its own.
- ccwrap reads its settings from environment variables, so it never takes a flag that belongs to
  `claude`: `CCWRAP_HOME` (default `~/.ccwrap`, on Windows too) and `CCWRAP_OPEN=0` (do not open the
  browser).

## Layout on disk

```
$CCWRAP_HOME/recordings/PROJECT/STAMP/   one recording
    0001.json, 0002.json ...             one API call each, written by the proxy
    ccwrap.json                          written by the launcher, see below
    proxy.log, viewer.log                the output of the two child processes
    context_profiler.html                written when the session ends
```

- PROJECT is the basename of the directory that ccwrap started in. STAMP is `YYYYMMDD-HHMMSS`.
- The launcher chooses the run directory and gives it to the proxy and the viewer.
- `ccwrap.json` holds `{"project": absolute path, "upstream": URL, "started": ISO 8601}`. The
  launcher writes it before it starts the proxy.

## Parts

The package is in `src/ccwrap/`. It uses the Python standard library only and needs Python 3.10 or
later. It runs on macOS, Linux and Windows.

- **launcher** (`cli.py`): finds `claude` with `shutil.which`, which also finds `claude.cmd` on
  Windows, starts the proxy and the viewer as child processes, sets `ANTHROPIC_BASE_URL`, runs
  `claude`, and stops the children when `claude` exits. It stops the proxy first, so that no call
  lands after the viewer writes the final page. If the viewer died, the launcher builds the page
  itself with `profiler.build()`. It prints the path of the page, or, if the session made no API
  call, one line that says so.
  - Ports: both children get `--port 0`, and the launcher reads the real port from the first line
    each child prints.
  - The proxy prints one line per API call, so its output goes to a log file, never to the terminal
    that `claude` owns.
  - Stopping: on POSIX, SIGTERM, then wait, then kill. On Windows, start the children with
    `CREATE_NEW_PROCESS_GROUP`, send `CTRL_BREAK_EVENT`, wait, then `terminate()`. A plain
    `terminate()` kills the viewer before it writes the final page.
- **proxy** (`proxy.py`): forwards each request upstream byte for byte and writes a copy to the run
  directory. It sits in the critical path of the session, so it stays dumb: it parses nothing,
  rewrites nothing and decides nothing. It redacts the auth headers, so credentials never reach the
  disk. The upstream is the user's own `ANTHROPIC_BASE_URL` if one is set, otherwise
  `https://api.anthropic.com`.
- **viewer** (`serve.py`): serves the profiler page from the run directory. It rebuilds the page when
  a new call lands, and the page polls `/status` and reloads itself. The viewer is a separate process
  from the proxy, so a slow or broken renderer never touches the session. Routes: `/` and
  `/context_profiler.html` serve the page, and `/status` returns `{"count": N}`.
- **reader** (`reader.py`): loads a run directory and pairs each request with its response.
- **profiler** (`profiler.py`): builds `context_profiler.html` for a run. It shows a tree of each
  request with characters and estimated tokens per node, and it has the selection panel that makes
  the prompt. Do not call it `profile.py`, because that name shadows the standard library's
  `profile` module.
- **switches** (`switches.py`): finds the switchable items in the requests and maps each one to its
  change, reading `~/.claude` for names only.

## Recording is best effort

A broken recorder must never cost the user a session. If the proxy cannot start (no free port, a
directory that cannot be written, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or
`CLAUDE_CODE_USE_FOUNDRY` set, which bypass any base URL), ccwrap prints one line that says why,
removes the run directory, and runs `claude` without recording. If only the viewer cannot start,
the recording goes on without the live page, and the launcher builds the page when `claude` exits.
Errors that ccwrap prints are one readable line, not a traceback.

## The prompt

The page knows exactly how to switch each item off. The prompt it builds gives Claude the exact
changes, and Claude only carries them out: it merges them into `~/.claude/settings.json`, shows the
user what it will change and asks before it writes, and leaves every other key and every other file
alone. Claude does not decide anything, so the same selection always gives the same change. Keep the
mapping from item to setting in `switches.py`, in one place:

| Ticked item                   | Change in `~/.claude/settings.json`                              |
|-------------------------------|------------------------------------------------------------------|
| built-in tool                 | add its bare name to `permissions.deny`                          |
| the Artifact tool             | add `Artifact` to `permissions.deny`; this removes `ArtifactComments` and `ArtifactData` too |
| one MCP tool                  | add `mcp__SERVER__TOOL` to `permissions.deny`                    |
| an MCP server                 | add `mcp__SERVER` to `permissions.deny`                          |
| one claude.ai connector       | add `mcp__claude_ai_NAME` to `permissions.deny`                  |
| all claude.ai connectors      | `"disableClaudeAiConnectors": true`                              |
| a plugin                      | `"NAME@MARKETPLACE": false` in `enabledPlugins`                  |
| a user skill                  | `"NAME": "off"` in `skillOverrides`                              |
| the bundled skills            | `"disableBundledSkills": true`                                   |
| a subagent type               | add `Agent(NAME)` to `permissions.deny`                          |

Prefer `permissions.deny` where it works: the user can remove an entry mid-session and the tool
comes back on the next request without breaking the prompt cache (recorded on Claude Code 2.1.306).
That is why the Artifact tool is not switched off with `enableArtifact: false`, which another
settings file cannot turn back on, and why an MCP server is denied rather than removed from
`mcpServers`.

The source is https://code.claude.com/docs/en/ (permissions, settings, mcp, plugins, skills,
sub-agents). Re-check a row there before you change it. Known limits:

- A bare tool name in `permissions.deny` removes the tool from Claude's context. When tool search is
  active, the schema of a deferred tool can still be in the request. The page must not promise a
  saving that the next recording does not show.
- `switches.py` tells skills apart only by `~/.claude` (user skills) and installed plugins (plugin
  skills). Every other unprefixed skill, project and synced skills included, counts in the bundled
  group, and a prefixed skill that is not a plugin's is not listed. `disableBundledSkills` may leave
  some listed commands in place, so the page shows the bundled group's saving as a maximum.
- A plugin that the project's own settings enable stays on (project settings win); the page does not
  detect this yet.
- `skillOverrides` does not reach plugin skills. A plugin skill goes only with its whole plugin, so
  the page offers the plugin, not the single skill.
- ccwrap always reads and edits `~/.claude`. A `CLAUDE_CONFIG_DIR` the user set is not followed.

## Tests

`tests/` holds the unit tests of the reader, the profiler and the viewer. Run them with
`python -m unittest discover -s tests`. Keep them green when you change those parts. This project
adds no new tests and no CI for now: the owner tests on Windows by hand.
