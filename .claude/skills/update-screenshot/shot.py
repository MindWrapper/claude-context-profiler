"""Take docs/screenshot.png from a recording: the main conversation's last request, its tools open
and sorted by size, the three biggest the session never used ticked for the Disable prompt, and
the biggest of those in the right pane. The timeline above, opened, shows how the context grew, with the subagents' rows under the
main conversation.
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


def main_request(run_dir: Path) -> tuple[int, str, list[str]]:
    """The position of the main conversation's last request, and the path and pick keys ("tool: NAME")
    of its three biggest tools that the session never used, as the README advises to cut."""
    calls = [r for r in reader.load_run(run_dir) if reader.is_messages_call(r)]
    profile = profiler.build_profile(reader.build_exchanges(calls))
    position = next(c["last"] for c in profile["chains"] if c["kind"] == "main")
    tools = next((n for n in profile["requests"][position]["children"] if n["path"] == "tools"), None)
    key = lambda n: f"{n['pick']['kind']}: {n['pick']['name']}"  # noqa: E731
    unused = [n for n in tools["children"] if n.get("pick") and not profile["usage"].get(key(n))] if tools else []
    biggest = sorted(unused, key=lambda n: -n["chars"])
    if not biggest:
        raise SystemExit("shot: the main conversation's last request has no unused tools")
    return position, biggest[0]["path"], [key(n) for n in biggest[:3]]


def main() -> int:
    run_dir, out = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    html = (run_dir / "context_profiler.html").read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    position, path, picks = main_request(run_dir)
    # The page restores its view from sessionStorage; seed it before the page script runs.
    state = {"req": position, "path": path, "sortBySize": True, "timelineOpen": True,
             "open": {"request": True, "tools": True}, "picked": {key: True for key in picks}}
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
