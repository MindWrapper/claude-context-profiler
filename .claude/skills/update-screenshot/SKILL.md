---
name: update-screenshot
description: Retake docs/screenshot.png, the README's picture of the profiler page, from a fresh demo recording.
disable-model-invocation: true
---

# Update the README screenshot

The recording holds the session's working directory and environment, so the demo runs in a neutral
folder: `/tmp/demo`. It holds one small file, so Claude reads it and the session has a second
request that adds something to the first.

1. Record a short demo session. It makes real API calls.

   ```bash
   rm -rf /tmp/demo && mkdir -p /tmp/demo/my-project && cd /tmp/demo/my-project && \
     printf 'print("Hello from the demo project")\n' > hello.py && \
     CLAUDE_CONTEXT_PROFILER_HOME=/tmp/demo/home \
     PYTHONPATH=$REPO/src python3 -m claude_context_profiler \
     -p "What does hello.py print? Read it, then answer in one line." < /dev/null
   ```

2. Take the screenshot. It selects the last request that adds something to the one before, turns
   **Only new** on, and opens the first new part in the right pane:

   ```bash
   python3 $REPO/.claude/skills/update-screenshot/shot.py /tmp/demo/home/recordings/my-project/* \
     $REPO/docs/screenshot.png
   ```

3. Look at `docs/screenshot.png`. Done when it shows a timeline with several bars, **Only new** on,
   new parts with blue dots in the tree, a new part's text in the right pane, and nothing personal:
   no email, no home path, no user name.
4. Delete `/tmp/demo`, and show the screenshot to the owner before committing it.
