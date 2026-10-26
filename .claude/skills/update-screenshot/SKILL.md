---
name: update-screenshot
description: Retake docs/screenshot.png, the README's picture of the profiler page, from a fresh demo recording.
disable-model-invocation: true
---

# Update the README screenshot

The recording holds the session's working directory and environment, so the demo runs in a neutral
folder: `/tmp/demo`. It holds two small files, and Claude starts one Explore subagent for each, so
the timeline shows the subagents' rows under the main conversation. `ENABLE_CLAUDEAI_MCP_SERVERS=false`
keeps the owner's claude.ai connectors out of the recording.

1. Record a short demo session. It makes real API calls.

   ```bash
   rm -rf /tmp/demo && mkdir -p /tmp/demo/my-project && cd /tmp/demo/my-project && \
     printf 'print("Hello from the demo project")\n' > hello.py && \
     printf 'print("Goodbye from the demo project")\n' > bye.py && \
     ENABLE_CLAUDEAI_MCP_SERVERS=false CLAUDE_CONTEXT_PROFILER_HOME=/tmp/demo/home \
     PYTHONPATH=$REPO/src python3 -m claude_context_profiler \
     -p "Start two Explore subagents at once: one finds what hello.py prints, the other what bye.py prints. Then answer in one line." < /dev/null
   ```

2. Take the screenshot. It selects the main conversation's last request, opens its tools sorted by
   size, ticks the three biggest the session never used, and opens the biggest of them in the right
   pane:

   ```bash
   python3 $REPO/.claude/skills/update-screenshot/shot.py /tmp/demo/home/recordings/my-project/* \
     $REPO/docs/screenshot.png
   ```

3. Look at `docs/screenshot.png`. Done when it shows a timeline with two subagent rows under the
   main conversation, the main conversation's row lit across the bars, three unused tools ticked
   and **Disable prompt (3)**, a tool's text in the right pane, and nothing personal: no email, no
   home path, no user name, no connector names.
4. Delete `/tmp/demo`, and show the screenshot to the owner before committing it.
