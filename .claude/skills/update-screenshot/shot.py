"""Take docs/screenshot.png from a recording: the last request that adds something to the one before,
with Only new on, so the tree shows the new parts with their blue dots and the right pane shows the
first of them. The timeline above shows how the context grew.
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

# The profiler from this checkout, which builds the request trees the page shows.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from claude_context_profiler import profiler, reader  # noqa: E402

CHROMES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


def find_chrome() -> str:
    for candidate in CHROMES:
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    raise SystemExit("shot: no Chrome or Chromium found")


def last_with_news(run_dir: Path) -> tuple[int, str]:
    """The position of the last request that adds parts to the previous one, and its first new part."""
    calls = [r for r in reader.load_run(run_dir) if reader.is_messages_call(r)]
    requests = profiler.build_profile(reader.build_exchanges(calls))["requests"]
    for position in range(len(requests) - 1, -1, -1):
        new = requests[position]["info"]["new"]
        if new:
            return position, new[0]
    raise SystemExit("shot: no request adds anything to the one before; record a session with a tool call")


def main() -> int:
    run_dir, out = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    html = (run_dir / "context_profiler.html").read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    position, path = last_with_news(run_dir)
    # The page restores its view from sessionStorage; seed it before the page script runs.
    state = {"req": position, "path": path, "sortBySize": True, "onlyNew": True, "open": {"request": True, "messages": True}}
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
