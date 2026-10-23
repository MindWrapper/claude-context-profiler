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
    print(json.dumps({"systemMessage": f"context profiler: {sys.argv[1]}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
