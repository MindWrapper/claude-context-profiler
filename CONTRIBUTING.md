# Contributing

Thanks for helping. The profiler is small on purpose, and these rules keep it that way.

## Set up and check a change

There is nothing to install: clone the repo, and use Python 3.9 or later.

```bash
python -m unittest discover -s tests
```

Before merging, the maintainer runs the same tests in CI on Linux, macOS and Windows, with Python
3.9 and the latest Python. For a change to the launcher, the proxy or the page, also record a real
session and look at the page: the steps are in [.claude/rules/recording.md](.claude/rules/recording.md#check-a-change-here).

## Ground rules

- **Standard library only.** No dependencies, so the tool runs from a plain clone.
- **The proxy stays dumb.** It forwards each request as it is and keeps a copy; it changes nothing and decides nothing.
- **Everything stays local.** The page loads nothing from the internet, and credentials never reach
  the disk (`tests/test_proxy.py` checks this).
- **The profiler never changes the user's config itself.** It only builds a prompt the user can give
  to Claude.
- Rules for each part of the code are in [.claude/rules/](.claude/rules/), and the design is in [AGENTS.md](AGENTS.md).
  Coding agents read both; Claude Code loads them on its own.

## Pull requests

1. Fork the repo and make your change on a branch.
2. Keep one change per pull request. If a user would notice it, update `README.md` and add a line
   under **Unreleased** in `CHANGELOG.md` in the same pull request.
3. Open the pull request against `main`. It is squash-merged once CI passes and it has been reviewed.

Report security problems privately, as [SECURITY.md](SECURITY.md) describes. Everyone taking part
follows the [code of conduct](CODE_OF_CONDUCT.md).
