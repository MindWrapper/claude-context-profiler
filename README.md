# ccwrap

ccwrap is a launcher for Claude Code. It runs Claude Code on a named config profile, so you can
keep a lean setup next to your normal one. It also has a context profiler: it records a session
and shows what every request to the model is made of (system prompt, tool schemas, skills, MCP
servers, messages), in estimated tokens. You see what your setup costs before you type anything.

## The loop

1. Record a session on your normal config: `ccwrap -x`. Claude Code starts as usual.
2. The profiler page opens in your browser and follows the session as it goes.
3. In the page, tick the tools, MCP servers, connectors and plugins you do not need. The page
   shows the tokens each one costs.
4. Type a profile name. The page shows a prompt. Copy it and paste it into Claude. Claude creates
   the profile.
5. Sign in once on the new profile: `ccwrap NAME auth login`.
6. Record again on the profile to confirm the context is smaller: `ccwrap -x NAME`.

From then on, `ccwrap NAME` starts Claude Code on the lean profile.

## Install

You need Python 3.10 or later (uv fetches one if needed) and Claude Code on your PATH.

The repo is private for now. Collaborators with access install it from GitHub over SSH.

One-off run, nothing installed:

    uvx --from git+ssh://git@github.com/MindWrapper/ccwrap ccwrap -x

Install, so `ccwrap` stays on your PATH:

    uv tool install git+ssh://git@github.com/MindWrapper/ccwrap
    uv tool upgrade ccwrap          # to update

Once the project is public, this becomes `uv tool install ccwrap`.

## Usage

    ccwrap [claude args...]              plain claude
    ccwrap NAME [claude args...]         claude with CLAUDE_CONFIG_DIR=$CCWRAP_HOME/profiles/NAME
    ccwrap -x [NAME] [claude args...]    the same, recorded, with the profiler page live in the browser
    ccwrap --ccwrap-help                 the ccwrap options and environment variables

- `-x` (or `--xray`) comes first. Only the word right after it can be the profile name, and only
  if it does not start with `-`. Everything after that goes to `claude` unchanged.
- A bare word in the name position is a profile name if it is a valid one: letters, digits, `.`,
  `_`, `-`, not starting with `-` or `_`. Any other word, such as a quoted prompt, goes to
  `claude`.
- If the profile does not exist, ccwrap stops and lists the existing ones. It never creates a
  profile itself, so a typo cannot make a new one. Profiles are made from the page.
- ccwrap takes no flags of its own besides `-x` and `--ccwrap-help`, so it never clashes with a
  `claude` flag. Settings come from the environment:

| Variable      | Meaning                                                              |
|---------------|----------------------------------------------------------------------|
| `CCWRAP_HOME` | where profiles and recordings live (default `~/.ccwrap`)             |
| `CCWRAP_PORT` | the first port to try: the proxy uses N, the viewer N+1 (default: free ports) |
| `CCWRAP_OPEN` | `0` keeps the browser closed (default: open it at the first API call) |

## Where things live

    ~/.ccwrap/profiles/NAME/                      a Claude Code config directory
    ~/.ccwrap/recordings/PROFILE/PROJECT/STAMP/   one recording

PROFILE is the profile name, or `_default` for your own config. PROJECT is the name of the
directory you started in. STAMP is `YYYYMMDD-HHMMSS`. The page of a recording is
`context_profiler.html` in its directory. The recording also holds one `NNNN.json` per API
call, a `ccwrap.json` with the run settings, and the proxy and viewer logs.

To remove a profile, delete its directory. A profile is a copy of your config, not a link: it
does not follow later changes to `~/.claude`. It copies settings, `CLAUDE.md`, agents, commands,
skills, hooks, plugins, keybindings and the MCP server list. Transcripts, history and
credentials stay behind, which is why you sign in once per profile.

## Privacy

A recording contains your prompts, the answers and the contents of files that Claude read.
Treat the recording directory like your transcripts. Auth headers are redacted before anything
is written. Nothing leaves your machine except the normal API traffic: the page is a local file
served on `127.0.0.1`.

## Limits

- Recording works by pointing Claude Code at a local proxy through `ANTHROPIC_BASE_URL`. It does
  not work with Bedrock, Vertex or Foundry, which ignore the base URL. If one of those is set,
  ccwrap says so and runs Claude Code without recording.
- Recording is best effort. If the proxy cannot start, ccwrap says why and runs Claude Code
  without it. If only the live page cannot start, the page is built when the session ends.
- Tool search: denying a tool removes it from Claude's context, but when tool search is active
  the schema of a deferred tool can still be in the request. Check the next recording for the
  real saving.
- The page cannot switch these off yet and shows them as not switchable: project skills and
  commands, synced claude.ai skills, and a plugin that the project's own settings enable (project
  settings win over the profile).
- The bundled skills saving is a maximum: some listed commands may stay.
- A plugin skill goes only with its whole plugin, so the page offers the plugin, not the skill.
- Local-scope MCP servers (under `projects` in `.claude.json`) are not copied to a profile.
- Token counts are estimates.
- Windows is supported, but tested by hand.

## How it works

ccwrap starts three things around `claude`:

- A proxy. It forwards each request upstream byte for byte and writes a copy to the recording
  directory. It sits in the path of your session, so it is kept dumb: it parses nothing, rewrites
  nothing and decides nothing. It redacts the auth headers.
- A viewer. A separate process that serves the page and rebuilds it when a new call lands. A
  slow or broken renderer can never touch your session.
- A profiler. It reads the recording, builds a tree of each request with estimated tokens per
  node, and builds the selection panel and the profile prompt. The mapping from an item to the
  setting that switches it off is in one file, `src/ccwrap/switches.py`.

When `claude` exits, ccwrap stops the proxy first, then the viewer, which writes the final page.
The package uses only the Python standard library.

## Development

    python -m unittest discover -s tests

Run from a checkout:

    uv run ccwrap
    PYTHONPATH=src python -m ccwrap

See `AGENTS.md` for the design notes.

## License

MIT.
