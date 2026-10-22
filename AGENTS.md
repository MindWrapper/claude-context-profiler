# claude-context-profiler

`claude-context-profiler` is a context profiler for Claude Code. It records one session and shows
what every request to the model is made of: each part, its estimated tokens and its full text. The
profiler page opens in the browser and follows the session live.

The profiler only shows. It changes nothing in the user's config and does not suggest what to
switch off; what to do about the context is up to the user. The page lets the user tick tools,
skills, subagent types and MCP servers and copy their kinds and names, one `kind: name` per line,
and that is all.
The README has a short, static guide to the usual settings.

`README.md` is the user-facing spec. Read `docs/common-guidelines-code.md` before you write code. It
applies to all code here.

This is a bare first version. It covers the main case only; the owner grows it step by step.

## Command line

```
ccprof [claude args...]   claude, recorded, with the profiler page live in the browser
```

- `ccprof` and `claude-context-profiler` are the same command; the README uses `ccprof`.
- Every argument goes to `claude` unchanged. The command has no options of its own.
- It reads its settings from environment variables, so it never takes a flag that belongs to
  `claude`: `CLAUDE_CONTEXT_PROFILER_HOME` (default `~/.claude-context-profiler`, on Windows too)
  and `CLAUDE_CONTEXT_PROFILER_OPEN=0` (do not open the browser).

## Layout on disk

```
$CLAUDE_CONTEXT_PROFILER_HOME/recordings/PROJECT/STAMP/   one recording
    0001.json, 0002.json ...             one API call each, written by the proxy
    run.json                          written by the launcher, see below
    proxy.log, viewer.log                the output of the two child processes
    context_profiler.html                written when the session ends
```

- PROJECT is the basename of the directory that the command started in. STAMP is
  `YYYYMMDD-HHMMSS`.
- The launcher chooses the run directory and gives it to the proxy and the viewer.
- `run.json` holds `{"project": absolute path, "upstream": URL, "started": ISO 8601}`. The
  launcher writes it before it starts the proxy.

## Parts

The package is in `src/claude_context_profiler/`. It uses the Python standard library only and needs
Python 3.10 or later. It runs on macOS, Linux and Windows.

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
  request with characters and estimated tokens per node, and the full text of the selected node.
  The listings that Claude Code puts into the prompt (skills, subagent types, deferred tools, MCP
  server instructions) are split into one node per item, in `LISTINGS`. Each tool and listed item
  carries a `pick` of kind and name, which the page shows as a checkbox.
  Do not call it `profile.py`, because that name shadows the standard library's `profile` module.

## Recording is best effort

A broken recorder must never cost the user a session. If the proxy cannot start (no free port, a
directory that cannot be written, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or
`CLAUDE_CODE_USE_FOUNDRY` set, which bypass any base URL), the launcher prints one line that says
why, removes the run directory, and runs `claude` without recording. If only the viewer cannot start,
the recording goes on without the live page, and the launcher builds the page when `claude` exits.
Errors that the profiler prints are one readable line, not a traceback.

## Tests

`tests/` holds the unit tests of the reader, the profiler, the viewer and the proxy. Run them with
`python -m unittest discover -s tests`. Keep them green when you change those parts.
`test_proxy.py` runs the real proxy between a test client and a fake upstream, and checks the
README's privacy promise: credentials go upstream but never reach the disk. Add tests only when the
owner asks; there is no CI for now, and the owner tests on Windows by hand.
