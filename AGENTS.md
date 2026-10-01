# ccwrap

`ccwrap` launches Claude Code. It can launch it on a named config profile, and it can record the
session to show what every request to the model is made of. The loop it exists for:

1. `ccwrap -x` records a session on the user's normal config. The context profiler page opens in
   the browser and follows the session live.
2. In the page, the user ticks the tools, MCP servers, connectors and plugins they do not need,
   sees the tokens each one costs, and types a profile name.
3. The page shows a prompt. The user pastes it into Claude, and Claude creates the profile.
4. `ccwrap -x <name>` records the lean profile, so the user can see the smaller context.

Read `docs/common-guidelines-code.md` before you write code. It applies to all code here.

## Command line

```
ccwrap [claude args...]           plain claude
ccwrap NAME [claude args...]      claude with CLAUDE_CONFIG_DIR=$CCWRAP_HOME/profiles/NAME
ccwrap -x [NAME] [claude args...] the same, recorded, with the profiler page live in the browser
ccwrap --ccwrap-help              the ccwrap options and environment variables
```

- `-x` / `--xray` comes first. The first word after it that does not start with `-` is the profile
  name. Everything after the profile name goes to `claude` unchanged.
- The grammar has no reserved words. Every bare first word is a profile name.
- If profile NAME does not exist, ccwrap stops with an error that lists the existing profiles and
  explains how the page makes one. It never creates a profile on its own, so a typo cannot create
  a new profile.
- ccwrap reads its settings from environment variables, so it never takes a flag that belongs to
  `claude`: `CCWRAP_HOME` (default `~/.ccwrap`, on Windows too), `CCWRAP_PORT` (the first port to
  try), `CCWRAP_OPEN=0` (do not open the browser).

## Layout on disk

```
$CCWRAP_HOME/profiles/NAME/                      a CLAUDE_CONFIG_DIR, incl. its own .claude.json
$CCWRAP_HOME/recordings/PROFILE/PROJECT/STAMP/   one recording
    0001.json, 0002.json ...                     one API call each, written by the proxy
    ccwrap.json                                  written by the launcher, see below
    context_profiler.html                        written when the session ends
```

- PROFILE is the profile name, or `_default` for the user's own config. PROJECT is the basename of
  the directory that ccwrap started in. STAMP is `YYYYMMDD-HHMMSS`.
- The launcher chooses the run directory and gives it to the proxy and the viewer.
- `ccwrap.json` holds `{"profile": NAME or null, "config_dir": absolute path, "project": absolute
  path, "started": ISO 8601}`. The page reads `config_dir` to know which config a new profile copies
  from.

## Parts

The package is in `src/ccwrap/`. It uses the Python standard library only and needs Python 3.10 or
later. It runs on macOS, Linux and Windows.

- **launcher** (`cli.py`): parses the command line, finds `claude` with `shutil.which`, which also
  finds `claude.cmd` on Windows, starts the proxy and the viewer as child processes, sets
  `ANTHROPIC_BASE_URL`, runs `claude`, and stops the children when `claude` exits. It stops the proxy
  first, so that no call lands after the viewer writes the final page. If the viewer died, the
  launcher builds the page itself. It always prints the path of the page.
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
  the profile prompt. Do not call it `profile.py`, because that name shadows the standard library's
  `profile` module.

## Recording is best effort

A broken recorder must never cost the user a session. If the proxy or the viewer cannot start
(no free port, a directory that cannot be written, `CLAUDE_CODE_USE_BEDROCK` or
`CLAUDE_CODE_USE_VERTEX` set, which bypass any base URL), ccwrap prints one line that says why and
runs `claude` without recording. Errors that ccwrap prints are one readable line, not a traceback.

## The profile prompt

The page knows exactly how to switch each item off. The prompt it builds gives Claude the exact
changes, and Claude only carries them out: it copies files, merges settings, and asks for the
permissions it needs. Claude does not decide anything, so the same selection always gives the same
profile. Keep the mapping from item to setting in the profiler, in one place:

| Ticked item               | Change in the new profile                                      |
|---------------------------|----------------------------------------------------------------|
| built-in tool             | add its name to `permissions.deny` in `settings.json`          |
| one MCP tool              | add `mcp__SERVER__TOOL` to `permissions.deny`                  |
| a whole MCP server        | leave the server out of `mcpServers` in `.claude.json`         |
| claude.ai connectors      | `"disableClaudeAiConnectors": true` in `settings.json`         |
| a plugin, or its skills   | set the plugin to `false` in `enabledPlugins`                  |

Check each mechanism against the current Claude Code documentation before you rely on it, and
correct this table if the documentation disagrees.

A new profile is a **copy**, not a link, of the config that the recording ran on (`config_dir` in
`ccwrap.json`). It copies only an allowlist:

- from the config directory: `settings.json`, `settings.local.json`, `CLAUDE.md` and the files it
  imports with `@`, `agents/`, `commands/`, `skills/`, `hooks/`, `plugins/`, `keybindings.json`, and
  any status line script that `settings.json` refers to
- from `.claude.json` (for the user's own config, this file is `~/.claude.json`, next to
  `~/.claude`, not inside it): `mcpServers` and the onboarding flags only

Transcripts, history, caches, sessions and credentials stay behind. The user runs
`claude auth login` once in each new profile. If the target profile already exists, the prompt tells
Claude to stop and ask before it changes anything.

## Tests

`tests/` holds the unit tests of the reader, the profiler and the viewer. Run them with
`python -m unittest discover -s tests`. Keep them green when you change those parts. This project
adds no new tests and no CI for now: the owner tests on Windows by hand.
