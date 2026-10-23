---
paths:
  - "src/claude_context_profiler/cli.py"
  - "src/claude_context_profiler/proxy.py"
  - "src/claude_context_profiler/serve.py"
  - "src/claude_context_profiler/reader.py"
  - "tests/test_proxy.py"
  - "tests/test_launcher.py"
  - "tests/test_serve.py"
---

# Recording: launcher, proxy, viewer, reader

## Layout on disk

```
$CLAUDE_CONTEXT_PROFILER_HOME/recordings/PROJECT/STAMP/   one recording
    0001.json, 0002.json ...   one API call each, written by the proxy
    run.json                   written by the launcher before it starts the proxy
    proxy.log, viewer.log      the output of the two child processes
    context_profiler.html      written when the session ends
```

PROJECT is the basename of the directory the command started in; STAMP is `YYYYMMDD-HHMMSS`.

## The parts

- **launcher** (`cli.py`) starts the proxy and the viewer, runs `claude` with `ANTHROPIC_BASE_URL`
  pointing at the proxy, and stops the children when `claude` exits.
  - It stops the proxy first, so that no call lands after the viewer writes the final page. If the
    viewer died, it builds the page itself with `profiler.build()`.
  - The proxy's output goes to a log file, never to the terminal that `claude` owns.
  - On Windows, the children start with `CREATE_NEW_PROCESS_GROUP` and stop with
    `CTRL_BREAK_EVENT`: a plain `terminate()` kills the viewer before it writes the final page.
- **proxy** (`proxy.py`) forwards each request upstream byte for byte and writes a copy. It sits in
  the critical path of the session, so it stays dumb: it parses nothing, rewrites nothing and
  decides nothing. It redacts the auth headers, so credentials never reach the disk;
  `tests/test_proxy.py` guards that promise from the README.
- **viewer** (`serve.py`) serves the page and rebuilds it when a call lands. It is a separate
  process from the proxy, so a slow or broken renderer never touches the session.
- **reader** (`reader.py`) loads a run directory and pairs each request with its response.

## Recording is best effort

A broken recorder must never cost the user a session. If the proxy cannot start (no free port, a
directory that cannot be written, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or
`CLAUDE_CODE_USE_FOUNDRY` set, which bypass any base URL), the launcher prints one line that says
why, removes the run directory, and runs `claude` without recording. If only the viewer cannot start,
the recording goes on without the live page, and the launcher builds the page when `claude` exits.

## Check a change here

`tests/test_launcher.py` runs the whole launcher end to end: a fake `claude` on the PATH sends a
request through the real proxy to a fake API, checks the live page, and a fake browser (the `BROWSER`
variable) records the address the viewer opens. CI runs it on Linux, macOS and Windows.

After the tests pass, record a real one-line session from an empty temporary folder and look at
what it wrote:

```bash
CLAUDE_CONTEXT_PROFILER_HOME=$TMP/home CLAUDE_CONTEXT_PROFILER_OPEN=0 \
  PYTHONPATH=$REPO/src python3 -m claude_context_profiler -p "say ok" < /dev/null
```

`< /dev/null` matters: without it, `claude -p` waits three seconds for input. The last line of output
is the path of the page. The session makes real API calls.
