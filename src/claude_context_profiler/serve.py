"""Show a recording in the browser while the session is still running.

The proxy drops one JSON file per API call into the run directory. This viewer
serves the profiler page built from those files, rebuilt whenever a new file
lands, and the page reloads itself when that happens. So the page in the
browser follows the session as it goes.

The viewer is a separate process from the proxy on purpose. The proxy sits in
the critical path of the session and must stay dumb; the rendering, which is
allowed to be slow or to fail, happens here. A broken viewer costs a page in the
browser and nothing else.

When the viewer stops, it writes the final page into the run directory, so the
recording reads the same way afterwards.

Routes:
    /, /context_profiler.html   what one call is made of
    /status                     {"count": N}, the number of recorded calls

    python -m claude_context_profiler.serve RUN [--port N] [--open]
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import profiler
from .reader import esc

DEFAULT_PORT = 8790

# Injected into every served page. Polls /status; reloads when a call landed;
# when the viewer is gone, says where the static copy of the page is.
LIVE_JS = r"""
(function () {
    var count = __COUNT__, saved = __SAVED__;
    var pill = document.createElement('div');
    pill.id = 'live';
    pill.style.cssText = 'position:fixed;top:.6rem;right:.8rem;z-index:9;padding:.3rem .6rem;border-radius:999px;' +
        'font:600 12px/1.4 ui-monospace,monospace;background:#0a7b34;color:#fff;box-shadow:0 1px 4px rgba(0,0,0,.3)';
    pill.textContent = 'live · ' + count + ' call' + (count === 1 ? '' : 's');
    document.body.appendChild(pill);
    function ended() {
        pill.style.background = '#666';
        pill.textContent = 'session ended · saved to ' + saved;
        pill.title = saved;
    }
    function tick() {
        fetch('/status', { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (s) {
                if (s.count !== count) { location.reload(); return; }
                setTimeout(tick, 1000);
            })
            .catch(ended);
    }
    setTimeout(tick, 1000);
})();
"""

WAITING_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>claude-context-profiler __RUN__</title>
<style>
:root { color-scheme: light dark; }
body { margin:0; padding:4rem 1.5rem; font:15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:640px; margin:0 auto; }
h1 { font-size:1.4rem; }
p { color:#888; }
</style></head>
<body><main>
<h1>Waiting for the first API call</h1>
<p>The session is recording to <code>__RUN__</code>. As soon as Claude Code sends its first request,
this page becomes the recording, and it follows the session from then on.</p>
</main></body></html>
"""


def count_records(run_dir: Path) -> int:
    """Complete files only. The proxy writes a .part and renames it when done."""
    return sum(1 for _ in run_dir.glob("[0-9]*.json"))


class Pages:
    """Renders the page for the current state of the run, once per state."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.lock = threading.Lock()
        self.cache: dict[int, str] = {}

    def page(self, count: int) -> str:
        with self.lock:
            if count not in self.cache:
                # One state, one render. The cache is keyed by count, so the
                # previous state's page goes as soon as a new call has landed.
                self.cache = {count: profiler.render_run(self.run_dir)}
            return self.cache[count]

    def live(self, count: int) -> str:
        waiting = WAITING_HTML.replace("__RUN__", esc(str(self.run_dir)))
        try:
            body = self.page(count) if count else waiting
        except ValueError:
            body = waiting  # calls landed, but no messages call among them yet
        saved = json.dumps(str(self.run_dir.resolve() / profiler.PAGE_NAME))
        script = LIVE_JS.replace("__COUNT__", str(count)).replace("__SAVED__", saved)
        return body.replace("</body>", f"<script>{script}</script></body>")

    def write_final(self) -> int:
        """The static page, as profiler.build would write it."""
        count = count_records(self.run_dir)
        if not count:
            return 0
        try:
            page = self.page(count)
        except ValueError:
            return 0
        (self.run_dir / profiler.PAGE_NAME).write_text(page, encoding="utf-8")
        return count


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-context-profiler-viewer"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - base class name
        pass

    def reply(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - base class name
        pages: Pages = self.server.pages
        path = self.path.split("?", 1)[0]
        count = count_records(pages.run_dir)
        if path == "/status":
            self.reply(200, json.dumps({"count": count}), "application/json")
            return
        if path not in ("/", "/" + profiler.PAGE_NAME):
            self.reply(404, "not found", "text/plain")
            return
        try:
            self.reply(200, pages.live(count))
        except Exception:  # noqa: BLE001 - a renderer bug must show in the browser, not kill the viewer
            self.reply(500, f"<pre>{esc(traceback.format_exc())}</pre>")


def open_on_first_call(run_dir: Path, url: str, stop: threading.Event) -> None:
    """Open the browser when there is something to show, not before: opening it
    at startup would take the focus from the terminal just as the session starts."""
    while not stop.is_set():
        if count_records(run_dir):
            webbrowser.open(url)
            return
        stop.wait(0.5)


def install_shutdown(server: ThreadingHTTPServer) -> None:
    # See proxy.py: the handler must not call shutdown() on the serving thread,
    # and a background job inherits SIGINT as ignored unless it is set here.
    # On Windows only SIGBREAK (CTRL_BREAK_EVENT) stops the viewer cleanly;
    # terminate() kills it before it writes the final page.
    def handle(signum, frame):  # noqa: ARG001 - signal handler signature
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="run directory")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open the browser at the first recorded call")
    args = parser.parse_args()

    run_dir = Path(args.run)
    if not run_dir.is_dir():
        print(f"viewer: no such run directory: {run_dir}", file=sys.stderr)
        return 1

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        print(f"viewer: cannot start: {error}", file=sys.stderr)
        return 1
    server.daemon_threads = True
    server.pages = Pages(run_dir)

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"viewer: {url} shows {run_dir} as it goes", flush=True)

    stop = threading.Event()
    if args.open:
        threading.Thread(target=open_on_first_call, args=(run_dir, url, stop), daemon=True).start()

    install_shutdown(server)
    server.serve_forever()
    server.server_close()
    stop.set()

    # The proxy may still be finishing its last file. Give it a moment.
    time.sleep(0.2)
    try:
        count = server.pages.write_final()
    except Exception as error:  # noqa: BLE001 - one line, not a traceback; the launcher can rebuild the page
        print(f"viewer: cannot write the final page: {error}", file=sys.stderr)
        return 1
    if count:
        print(f"viewer: {count} calls -> {run_dir / profiler.PAGE_NAME}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
