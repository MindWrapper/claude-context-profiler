# claude-context-profiler

`claude-context-profiler` is a context profiler for Claude Code. It records one session and shows
what every request to the model is made of: each part, its estimated tokens and its full text. The
profiler page opens in the browser and follows the session live.

The profiler only shows. It changes nothing in the user's config and does not suggest what to
switch off; what to do about the context is up to the user. The page lets the user tick tools,
skills, subagent types and MCP servers and copy their kinds and names, one `kind: name` per line,
and that is all. The README has a short, static guide to the usual settings.

`README.md` is the user-facing spec. This is a bare first version: it covers the main case only, and
the owner grows it step by step.

## Working here

- When a request is ambiguous, ask before you build. Name inconsistencies and tradeoffs you find,
  and push back when a request conflicts with the spec or the design below.
- Plan inline before you code. For a change you can describe in one sentence, just make it.
- Change only what the task needs; leave unrelated code and comments as they are.
- Write the simple, correct version first. Optimize only when a measurement asks for it.
- Language rules: `.claude/rules/python.md` for Python, `.claude/rules/page.md` for the page's HTML,
  CSS and JavaScript (they live inside `profiler.py` and `serve.py`). Claude Code loads them when
  it touches those files; other agents read them before editing.

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
    0001.json, 0002.json ...   one API call each, written by the proxy
    run.json                   written by the launcher before it starts the proxy
    proxy.log, viewer.log      the output of the two child processes
    context_profiler.html      written when the session ends
```

PROJECT is the basename of the directory the command started in; STAMP is `YYYYMMDD-HHMMSS`.

## Parts

The package is in `src/claude_context_profiler/`: Python standard library only, Python 3.9 or later
(the `python3` that macOS ships), on macOS, Linux and Windows.

- **launcher** (`cli.py`) starts the proxy and the viewer, runs `claude` with `ANTHROPIC_BASE_URL`
  pointing at the proxy, and stops the children when `claude` exits.
  - It stops the proxy first, so that no call lands after the viewer writes the final page. If the
    viewer died, it builds the page itself with `profiler.build()`.
  - The proxy's output goes to a log file, never to the terminal that `claude` owns.
  - On Windows, the children start with `CREATE_NEW_PROCESS_GROUP` and stop with
    `CTRL_BREAK_EVENT`: a plain `terminate()` kills the viewer before it writes the final page.
- **proxy** (`proxy.py`) forwards each request upstream byte for byte and writes a copy. It sits in
  the critical path of the session, so it stays dumb: it parses nothing, rewrites nothing and
  decides nothing. It redacts the auth headers, so credentials never reach the disk.
- **viewer** (`serve.py`) serves the page and rebuilds it when a call lands. It is a separate
  process from the proxy, so a slow or broken renderer never touches the session.
- **reader** (`reader.py`) loads a run directory and pairs each request with its response.
- **profiler** (`profiler.py`) builds the page: a tree of each request with estimated tokens per
  node. The listings Claude Code puts into the prompt (skills, subagent types, deferred tools, MCP
  server instructions) are split into one node per item by `LISTINGS`; each tool and listed item
  carries a `pick`, which the page shows as a checkbox. The module is named `profiler.py` because
  `profile.py` would shadow the standard library's `profile` module.

## Recording is best effort

A broken recorder must never cost the user a session. If the proxy cannot start (no free port, a
directory that cannot be written, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or
`CLAUDE_CODE_USE_FOUNDRY` set, which bypass any base URL), the launcher prints one line that says
why, removes the run directory, and runs `claude` without recording. If only the viewer cannot start,
the recording goes on without the live page, and the launcher builds the page when `claude` exits.
Errors that the profiler prints are one readable line, not a traceback.

## Check your change

1. Run the tests: `python3 -m unittest discover -s tests`. They must pass before you call a change
   done. `test_proxy.py` guards the README's privacy promise: credentials go upstream but never
   reach the disk.
2. For a change to the launcher, proxy, viewer or page, record a real one-line session from an empty
   temporary folder and look at what it wrote:

   ```bash
   CLAUDE_CONTEXT_PROFILER_HOME=$TMP/home CLAUDE_CONTEXT_PROFILER_OPEN=0 \
     PYTHONPATH=$REPO/src python3 -m claude_context_profiler -p "say ok" < /dev/null
   ```

   `< /dev/null` matters: without it, `claude -p` waits three seconds for input. The last line of
   output is the path of the page.
3. For a change to the page, serve the recording with
   `python3 -m claude_context_profiler.serve RUN_DIR --port 8791`, open it in a browser, and check
   the console for errors. After a JavaScript edit, extract the page's last `<script>` and run
   `node --check` on it.

## Repository etiquette

- Work on `main`. Commit and push only when the owner asks.
- One change per commit. The subject starts with an imperative verb ("Add …", "Rename …").
- A change in behavior updates `README.md` and this file in the same commit.
- When the page's look changes, retake `docs/screenshot.png` with the `/update-screenshot` skill
  (`.claude/skills/update-screenshot/SKILL.md`).
- Add tests only when the owner asks. There is no CI; the owner tests on Windows by hand.
