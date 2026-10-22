---
name: update-screenshot
description: Retake docs/screenshot.png, the README's picture of the profiler page, from a fresh demo recording.
disable-model-invocation: true
---

# Update the README screenshot

The page's header prints the recording's path, so the demo runs in a neutral folder: `/tmp/demo`.

1. Record a one-line demo session. It makes real API calls.

   ```bash
   rm -rf /tmp/demo && mkdir -p /tmp/demo/my-project && cd /tmp/demo/my-project && \
     CLAUDE_CONTEXT_PROFILER_HOME=/tmp/demo/home CLAUDE_CONTEXT_PROFILER_OPEN=0 \
     PYTHONPATH=$REPO/src python3 -m claude_context_profiler \
     -p "What does this directory contain? Answer in one line." < /dev/null
   ```

2. Take the screenshot. It opens the first request's tools, sorted by size, ticks the three largest,
   and selects the Artifact tool (the largest tool, if there is none), so the right pane shows its
   text:

   ```bash
   python3 $REPO/.claude/skills/update-screenshot/shot.py /tmp/demo/home/recordings/my-project/* \
     $REPO/docs/screenshot.png
   ```

3. Look at `docs/screenshot.png`. Done when it shows the tools tree with three ticked items,
   **copy 3 selected**, the Artifact tool's text in the right pane, and nothing personal: no email,
   no home path, no user name.
4. Delete `/tmp/demo`, and show the screenshot to the owner before committing it.
