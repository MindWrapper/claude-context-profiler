"""Tests for the proxy: credentials go upstream, never to disk. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_context_profiler import proxy  # noqa: E402

SECRETS = {
    "Authorization": "Bearer FAKE-OAUTH-TOKEN",
    "X-Api-Key": "FAKE-API-KEY",
    "Cookie": "c=FAKE-COOKIE",
    "Proxy-Authorization": "Basic FAKE-PROXY-AUTH",
}
SET_COOKIE = "session=FAKE-SET-COOKIE"


class Upstream(BaseHTTPRequestHandler):
    """Answers every call with a small JSON body and a Set-Cookie, and keeps the
    headers it received so a test can see what the proxy forwarded."""

    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - base class name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).received.append(dict(self.headers.items()))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", SET_COOKIE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003 - base class name
        pass


def serve(server: ThreadingHTTPServer) -> None:
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()


class Redact(unittest.TestCase):
    def test_secret_headers_are_blanked_whatever_their_case(self):
        out = proxy.redact([("AUTHORIZATION", "a"), ("x-api-key", "b"), ("Set-Cookie", "c"), ("Accept", "d")])
        self.assertEqual(out, {"authorization": "<redacted>", "x-api-key": "<redacted>",
                               "set-cookie": "<redacted>", "accept": "d"})


class Recording(unittest.TestCase):
    def setUp(self):
        Upstream.received = []
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        serve(self.upstream)
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        self.proxy.config = proxy.Config(f"http://127.0.0.1:{self.upstream.server_address[1]}",
                                         proxy.Recorder(self.run_dir), 10)
        serve(self.proxy)
        # The proxy logs one line per call to stdout; keep it out of the test output.
        self.quiet = mock.patch.object(proxy, "print", create=True)
        self.quiet.start()

    def tearDown(self):
        self.quiet.stop()
        for server in (self.proxy, self.upstream):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def call(self, headers: dict) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=10)
        conn.request("POST", "/v1/messages", body=b'{"messages": []}',
                     headers={"Content-Type": "application/json", **headers})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        # The proxy answers first and writes the record right after.
        path = self.run_dir / "0001.json"
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_credentials_go_upstream_but_never_to_disk(self):
        record = self.call(SECRETS)

        # Forwarded unchanged: the session must still be able to sign in.
        sent = {k.lower(): v for k, v in Upstream.received[0].items()}
        for key in ("Authorization", "X-Api-Key", "Cookie"):
            self.assertEqual(sent[key.lower()], SECRETS[key])

        for key in SECRETS:
            self.assertEqual(record["request_headers"][key.lower()], "<redacted>")
        self.assertEqual(record["response_headers"]["set-cookie"], "<redacted>")

        on_disk = "".join(path.read_text(encoding="utf-8") for path in self.run_dir.iterdir())
        for secret in [*SECRETS.values(), SET_COOKIE]:
            self.assertNotIn(secret.split()[-1], on_disk)

    def test_custom_headers_are_recorded_as_they_are(self):
        # The README says so: only the known auth headers are blanked.
        record = self.call({"X-Gateway-Key": "FAKE-CUSTOM-HEADER"})
        self.assertEqual(record["request_headers"]["x-gateway-key"], "FAKE-CUSTOM-HEADER")


if __name__ == "__main__":
    unittest.main()
