"""End-to-end test of the launcher: ccprof runs a fake claude through the real proxy and viewer.

The fake claude sends one request to ANTHROPIC_BASE_URL, as the real one does, and checks the live
page while the session is still open. A fake API answers it, and a fake browser, set through the
standard BROWSER variable, records the address the viewer opens. Nothing leaves 127.0.0.1, so it
runs the same on Linux, macOS and Windows. Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
WINDOWS = os.name == "nt"

# The fake claude: one request through the proxy, then a look at the live page.
FAKE_CLAUDE = r'''
import glob, json, os, re, subprocess, sys, time, urllib.request

# Run the SessionStart hook from --settings the way claude does: in bash (Git Bash on Windows),
# with JSON on stdin.
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
bash = GIT_BASH if os.path.exists(GIT_BASH) else "bash"
greeting = None
if "--settings" in sys.argv:
    settings = json.load(open(sys.argv[sys.argv.index("--settings") + 1], encoding="utf-8"))
    for group in settings["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            out = subprocess.run([bash, "-c", hook["command"]], input="{}", capture_output=True, text=True)
            greeting = json.loads(out.stdout)["systemMessage"] if out.returncode == 0 else "hook failed: " + out.stderr

base = os.environ["ANTHROPIC_BASE_URL"]
body = {
    "model": "fake-model", "max_tokens": 10,
    "system": [{"type": "text", "text": "You are a fake."}],
    "tools": [{"name": "FakeTool", "description": "Does nothing.", "input_schema": {"type": "object"}}],
    "messages": [{"role": "user", "content": "hi"}],
}
request = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json", "x-api-key": "FAKE-API-KEY"})
answer = json.loads(urllib.request.urlopen(request, timeout=30).read())

result = {"answer": answer["content"][0]["text"], "status": None, "page_has_tree": False, "opened": None,
          "greeting": greeting}
deadline = time.monotonic() + 30
home = os.environ["CLAUDE_CONTEXT_PROFILER_HOME"]
while time.monotonic() < deadline:
    logs = glob.glob(os.path.join(home, "recordings", "*", "*", "viewer.log"))
    port = None
    for log in logs:
        found = re.search(r"http://127\.0\.0\.1:(\d+)/", open(log, encoding="utf-8").read())
        if found:
            port = found.group(1)
    if port:
        status = json.loads(urllib.request.urlopen("http://127.0.0.1:%s/status" % port, timeout=5).read())
        result["status"] = status["count"]
        if status["count"]:
            page = urllib.request.urlopen("http://127.0.0.1:%s/" % port, timeout=5).read().decode("utf-8")
            result["page_has_tree"] = '<div id="rows">' in page
    opened = os.environ["FAKE_BROWSER_LOG"]
    if os.path.exists(opened):
        result["opened"] = open(opened, encoding="utf-8").read().strip()
    if result["page_has_tree"] and result["opened"]:
        break
    time.sleep(0.25)

with open(os.environ["FAKE_CLAUDE_RESULT"], "w", encoding="utf-8") as out:
    json.dump(result, out)
'''


class FakeAPI(BaseHTTPRequestHandler):
    """Answers every call like the Messages API and keeps the headers it received."""

    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - base class name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).received.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps({
            "id": "msg_fake", "type": "message", "role": "assistant", "model": "fake-model",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 1},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003 - base class name
        pass


def write_command(folder: Path, name: str, posix: str, windows: str) -> Path:
    """A small executable: a .cmd file on Windows, a shell script elsewhere."""
    if WINDOWS:
        path = folder / f"{name}.cmd"
        path.write_text("@echo off\r\n" + windows + "\r\n", encoding="utf-8")
    else:
        path = folder / name
        path.write_text("#!/bin/sh\n" + posix + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Launcher(unittest.TestCase):
    def setUp(self):
        FakeAPI.received = []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        self.api.daemon_threads = True
        threading.Thread(target=self.api.serve_forever, daemon=True).start()

    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()
        self.tmp.cleanup()

    def test_records_a_session_and_serves_the_live_page(self):
        bin_dir, home, project = self.root / "bin", self.root / "home", self.root / "project"
        for folder in (bin_dir, home, project):
            folder.mkdir()
        script = bin_dir / "fake_claude.py"
        script.write_text(FAKE_CLAUDE, encoding="utf-8")
        write_command(bin_dir, "claude", f'exec "{sys.executable}" "{script}" "$@"', f'"{sys.executable}" "{script}" %*')
        opened_log = self.root / "opened.txt"
        browser = write_command(bin_dir, "fakebrowser", f'echo "$1" > "{opened_log}"', f'echo %1> "{opened_log}"')
        result_file = self.root / "result.json"

        env = dict(os.environ)
        env.update({
            "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
            "PYTHONPATH": str(SRC),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.api.server_address[1]}",
            "CLAUDE_CONTEXT_PROFILER_HOME": str(home),
            "BROWSER": str(browser),
            "FAKE_BROWSER_LOG": str(opened_log),
            "FAKE_CLAUDE_RESULT": str(result_file),
        })
        for name in ("CLAUDE_CONTEXT_PROFILER_OPEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                     "CLAUDE_CODE_USE_FOUNDRY", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(name, None)

        run = subprocess.run([sys.executable, "-m", "claude_context_profiler", "-p", "hi"], cwd=project, env=env,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=120)
        report = f"\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
        self.assertEqual(run.returncode, 0, report)
        self.assertIn("ccprof: live page at http://127.0.0.1:", run.stderr, report)
        self.assertIn("ccprof: page:", run.stderr, report)

        # What the fake claude saw while the session was live.
        result = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result["answer"], "ok", report)
        self.assertGreaterEqual(result["status"] or 0, 1, f"the live page saw no call: {result}{report}")
        self.assertTrue(result["page_has_tree"], f"the live page did not render: {result}{report}")
        self.assertTrue((result["opened"] or "").startswith("http://127.0.0.1:"),
                        f"the browser was not opened: {result}{report}")
        # Claude's start screen shows the address the launcher printed.
        page_url = run.stderr.split("ccprof: live page at ")[1].split()[0]
        greeting = result["greeting"] or ""
        self.assertIn(f"\x1b]8;;{page_url}\x1b\\", greeting, f"the address is not a link: {result}{report}")
        visible = re.sub(r"\x1b\]8;;[^\x1b]*\x1b\\", "", greeting)
        self.assertIn(f"Context profiler  \u2192  {page_url}", visible, f"{result}{report}")

        # The key went upstream, but the recording blanked it, and the final page was written.
        self.assertEqual(FakeAPI.received[0]["x-api-key"], "FAKE-API-KEY")
        run_dirs = list((home / "recordings" / "project").iterdir())
        self.assertEqual(len(run_dirs), 1, report)
        record = json.loads((run_dirs[0] / "0001.json").read_text(encoding="utf-8"))
        self.assertEqual(record["request_headers"]["x-api-key"], "<redacted>")
        page = (run_dirs[0] / "context_profiler.html").read_text(encoding="utf-8")
        self.assertIn('<div id="rows">', page)
        self.assertNotIn("FAKE-API-KEY", page)


if __name__ == "__main__":
    unittest.main()
