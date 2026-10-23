"""Take docs/screenshot.png from a recording: the first request's tools, sorted by size, with the
three largest that are sensible to switch off ticked and the Artifact tool selected, so the right
pane shows its text.
Usage: python3 shot.py RUN_DIR OUT.png
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHROMES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome",
    "chromium",
    "chromium-browser",
]

# Tools Claude Code works with all the time. The screenshot must not suggest switching them off.
CORE = {"Agent", "Task", "Bash", "Read", "Edit", "Write", "Glob", "Grep", "ToolSearch", "Skill", "TodoWrite"}


def find_chrome() -> str:
    for candidate in CHROMES:
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    raise SystemExit("shot: no Chrome or Chromium found")


def tools_by_size(run_dir: Path) -> list[str]:
    """The tool names of the first request that has tools, largest first."""
    for path in sorted(run_dir.glob("0*.json")):
        tools = (json.loads(path.read_text(encoding="utf-8")).get("request_json") or {}).get("tools") or []
        if tools:
            tools = sorted(tools, key=lambda tool: len(json.dumps(tool)), reverse=True)
            return [tool.get("name", "") for tool in tools]
    return []


def main() -> int:
    run_dir, out = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    html = (run_dir / "context_profiler.html").read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    tools = tools_by_size(run_dir)
    shown = "Artifact" if "Artifact" in tools else tools[0]
    # The page restores its view from sessionStorage; seed it before the page script runs.
    state = {
        "req": 0, "atEnd": False, "path": f"tools/{shown}", "sortBySize": True,
        "open": {"request": True, "tools": True},
        "picked": {f"tool: {name}": True for name in [name for name in tools if name not in CORE][:3]},
    }
    seed = "<script>sessionStorage.setItem(%s, %s);</script>" % (
        json.dumps("profile:" + title), json.dumps(json.dumps(state)))
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "shot.html"
        page.write_text(html.replace("</title>", "</title>" + seed, 1), encoding="utf-8")
        subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=2", "--window-size=1400,820",
             f"--screenshot={out}", page.as_uri()],
            check=True, capture_output=True,
        )
    print(f"shot: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
