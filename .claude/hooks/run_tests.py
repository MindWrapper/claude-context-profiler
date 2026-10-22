"""Stop hook: run the tests before Claude ends a turn that changed Python files.

Exit 2 with the failing output on stderr blocks the stop, and Claude Code shows that output to
Claude so it can fix the failure. A turn that changed no Python file passes untouched, and so does
a stop that a previous block already continued, so the hook cannot loop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def python_changed() -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    return any(line.rstrip().endswith(".py") for line in status.stdout.splitlines())


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if event.get("stop_hook_active") or not python_changed():
        return 0
    run = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=170,
    )
    if run.returncode == 0:
        return 0
    tail = "\n".join((run.stdout + run.stderr).splitlines()[-40:])
    print(f"The tests fail; fix them before you finish:\n{tail}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
