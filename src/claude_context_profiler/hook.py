"""The SessionStart hook ccprof hands to claude for one session.

It shows the address of the live page on Claude's start screen, as a systemMessage: the user sees
it, the model does not, so it leaves the recorded context as it is.

    python -m claude_context_profiler.hook URL
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    sys.stdin.read()  # claude sends the session's details; nothing here needs them
    # Claude Code puts its own "SessionStart:startup says:" before the message; starting with a
    # newline gives the box a line of its own. json.dumps escapes the box characters, so the output
    # is plain ASCII on any console code page.
    url = sys.argv[1]
    line = f"  Context profiler  \u2192  {url}  "
    # The address goes in a terminal hyperlink (OSC 8). Claude Code draws it as a real link; a
    # terminal without link support shows the address itself, which it can still pick out.
    link = f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"
    edge = "\u2500" * len(line)  # measured on the visible text, without the link codes
    box = f"\u256d{edge}\u256e\n\u2502{line.replace(url, link)}\u2502\n\u2570{edge}\u256f"
    print(json.dumps({"systemMessage": "\n" + box}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
