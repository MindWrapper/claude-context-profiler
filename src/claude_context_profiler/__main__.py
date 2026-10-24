"""python -m claude_context_profiler, the same as the claude-context-profiler command."""

import sys

from .cli import main

sys.exit(main())
