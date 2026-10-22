# claude-context-profiler

`claude-context-profiler` is a context profiler for Claude Code. It records one session and shows
what every request to the model is made of: each part, its estimated tokens and its full text. The
profiler page opens in the browser and follows the session live.

The profiler never changes the user's config itself, and does not suggest what to switch off; that
is the user's choice. The page lets the user tick tools, skills, subagent types and MCP servers and
copy a prompt that asks Claude to switch them off in `~/.claude/settings.json`. Tools and subagent
types become exact `permissions.deny` entries; for skills and MCP servers the prompt states the rule
and has Claude check with the user. Claude shows the change and asks before it writes.

`README.md` is the user-facing spec. This is a bare first version: it covers the main case only, and
the owner grows it step by step. The package is `src/claude_context_profiler/`: standard library
only, Python 3.9 or later, on macOS, Linux and Windows.

## Working here

- When a request is ambiguous, ask before you build. Name inconsistencies and tradeoffs you find,
  and push back when a request conflicts with the spec or the design.
- Plan inline before you code. For a change you can describe in one sentence, just make it.
- Change only what the task needs; leave unrelated code and comments as they are.
- Write the simple, correct version first. Optimize only when a measurement asks for it.

## Command line

```
ccprof [claude args...]   claude, recorded, with the profiler page live in the browser
```

`ccprof` and `claude-context-profiler` are the same command. Every argument goes to `claude`
unchanged; settings come from `CLAUDE_CONTEXT_PROFILER_HOME` (default `~/.claude-context-profiler`)
and `CLAUDE_CONTEXT_PROFILER_OPEN=0` (do not open the browser), so the command never takes a flag
that belongs to `claude`.

## Where the details are

Read the file that matches what you touch, before you change it. Claude Code loads the rules on its
own when it opens a matching file.

- `.claude/rules/python.md`: any Python file.
- `.claude/rules/recording.md`: the launcher, proxy, viewer or reader (`cli.py`, `proxy.py`,
  `serve.py`, `reader.py`): the layout on disk, how the parts start and stop, best-effort recording,
  and how to check a change with a real session.
- `.claude/rules/page.md`: the page (`profiler.py`, `serve.py`): its HTML, CSS and JavaScript, how
  the tree is built, and how to check the page.

## Check your change

Run `python3 -m unittest discover -s tests`; it must pass before you call a change done. CI runs the
same tests on Linux, macOS and Windows with Python 3.9 and the latest Python. A change to the
launcher, proxy or page also needs the real-session check in its rules file.

## Repository etiquette

- The owner works on `main` and commits and pushes only when the owner asks. Contributors open a
  pull request from a branch; it is squash-merged once CI passes. See `CONTRIBUTING.md`.
- One change per commit. The subject starts with an imperative verb ("Add …", "Rename …").
- A change in behavior updates `README.md` and the **Unreleased** section of `CHANGELOG.md` in the
  same commit, and this file or a rules file when the design changes.
- Add tests only when the owner asks.
