"""Record what Claude Code actually sends to the Anthropic API.

Claude Code talks to whatever ANTHROPIC_BASE_URL points at. Point it here: this
proxy forwards every request upstream untouched and writes a copy of the wire
traffic to RUN/NNNN.json, where RUN is the run directory the launcher chose.

The proxy stays dumb on purpose. It parses nothing, rewrites nothing and decides
nothing; it sits in the critical path of the session, so the only job it has is
to move bytes and drop a copy on disk. Reading the copies is the reader's job.

Credentials never reach the disk: the recording redacts the auth headers.

    python -m claude_context_profiler.proxy RUN [--port N] [--upstream URL] [--timeout S]
"""

from __future__ import annotations

import argparse
import http.client
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_PORT = 8788
CHUNK = 16384

# Headers that describe one hop and must not travel to the next one.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Never written to a recording.
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization", "set-cookie"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Recorder:
    """Numbers the exchanges of one run and writes them to the run directory."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.lock = threading.Lock()
        self.count = 0

    def next_index(self) -> int:
        with self.lock:
            self.count += 1
            return self.count

    def write(self, index: int, record: dict) -> None:
        path = self.run_dir / f"{index:04d}.json"
        tmp = path.with_suffix(".json.part")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
        tmp.rename(path)


def redact(headers) -> dict:
    out = {}
    for key, value in headers:
        out[key.lower()] = "<redacted>" if key.lower() in SECRET_HEADERS else value
    return out


def decode_body(raw: bytes, content_type: str) -> tuple[str, object]:
    """Return the field name and the value under which a body is stored."""
    text = raw.decode("utf-8", errors="replace")
    if "event-stream" in content_type:
        return "sse", text
    if "json" in content_type:
        try:
            return "json", json.loads(text)
        except json.JSONDecodeError:
            return "text", text
    return "text", text


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-context-profiler-proxy"
    sys_version = ""
    # A streamed answer arrives as many small frames. Nagle would hold each one
    # back waiting for company, and the session would feel slower than it is.
    disable_nagle_algorithm = True

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - base class name
        pass

    def read_request_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                body += self.rfile.read(size)
                self.rfile.readline()
            return bytes(body)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def forward_headers(self) -> dict:
        out = {}
        for key, value in self.headers.items():
            lower = key.lower()
            # accept-encoding is dropped here and set once below. Clients differ
            # in how they capitalize it, so letting the original through would
            # send two of them and leave the choice to the upstream.
            if lower in HOP_BY_HOP or lower in ("host", "accept-encoding"):
                continue
            out[key] = value
        # A compressed response would make the recording unreadable, and the tee
        # would have to inflate it. Ask upstream for plain bytes instead.
        out["Accept-Encoding"] = "identity"
        return out

    def fail(self, status: int, message: str) -> None:
        body = json.dumps({"type": "error", "error": {"type": "proxy_error", "message": message}})
        raw = body.encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # --- the exchange -----------------------------------------------------

    def proxy(self) -> None:
        cfg = self.server.config
        started = time.time()
        index = cfg.recorder.next_index()

        request_raw = self.read_request_body()
        request_type = self.headers.get("Content-Type") or ""
        req_field, req_value = decode_body(request_raw, request_type)

        record = {
            "index": index,
            "started_at": now_iso(),
            "method": self.command,
            "path": self.path,
            "request_headers": redact(self.headers.items()),
            f"request_{req_field}": req_value,
        }

        conn = cfg.connect()
        response_raw = bytearray()
        try:
            conn.request(self.command, cfg.prefix + self.path, body=request_raw, headers=self.forward_headers())
            upstream = conn.getresponse()

            record["status"] = upstream.status
            record["response_headers"] = redact(upstream.getheaders())

            self.send_response_only(upstream.status, upstream.reason)
            for key, value in upstream.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            while True:
                # read1, not read: read(n) blocks until it has n bytes, which
                # would hold a streamed answer back until the model finished.
                chunk = upstream.read1(CHUNK)
                if not chunk:
                    break
                response_raw += chunk
                # Written and flushed straight away: a streamed answer must
                # reach Claude Code as it arrives, not when it is complete.
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

            response_type = upstream.getheader("Content-Type") or ""
        except BrokenPipeError:
            # Claude Code hung up mid-stream, usually because you pressed Esc.
            record["aborted"] = "client disconnected"
            response_type = "text/event-stream"
        except OSError as error:
            record["error"] = f"{type(error).__name__}: {error}"
            response_type = ""
            try:
                self.fail(502, str(error))
            except OSError:
                pass
        finally:
            conn.close()

        if response_raw:
            field, value = decode_body(bytes(response_raw), response_type)
            record[f"response_{field}"] = value
        record["duration_ms"] = round((time.time() - started) * 1000)
        cfg.recorder.write(index, record)

        print(
            f"  {index:4d}  {self.command} {self.path}  "
            f"{record.get('status', record.get('error', '-'))}  {record['duration_ms']}ms",
            flush=True,
        )

    do_POST = proxy
    do_GET = proxy
    do_PUT = proxy
    do_DELETE = proxy
    do_PATCH = proxy


class Config:
    def __init__(self, upstream: str, recorder: Recorder, timeout: int) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("https", "http"):
            raise SystemExit(f"proxy: upstream must be http or https, got {upstream}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.recorder = recorder
        self.timeout = timeout

    def connect(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)


def install_shutdown(server: ThreadingHTTPServer) -> None:
    """Stop cleanly on SIGINT and SIGTERM, and on SIGBREAK on Windows.

    The handlers are set explicitly rather than left to the KeyboardInterrupt
    default, because a shell sets SIGINT to ignored for a background job and
    Python keeps an inherited SIG_IGN. Without this, a proxy started with "&"
    could not be stopped by SIGINT at all.

    On Windows, a parent stops a child cleanly with CTRL_BREAK_EVENT, which
    arrives as SIGBREAK when the child runs in its own process group. SIGTERM is
    never delivered there: terminate() kills the process outright. That is safe
    for the proxy, because every call is written to a .part file and renamed
    only when complete.

    shutdown() blocks until serve_forever() returns, so it cannot run in the
    handler itself: the handler runs on the very thread it would wait for.
    """

    def handle(signum, frame):  # noqa: ARG001 - signal handler signature
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="run directory to write the calls to; created if missing")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    parser.add_argument("-u", "--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--timeout", type=int, default=900, help="upstream timeout in seconds")
    args = parser.parse_args()

    # Anything that can fail before the proxy listens fails with one line, not a
    # traceback: the launcher shows this output to whoever started the session.
    try:
        run_dir = Path(args.run)
        run_dir.mkdir(parents=True, exist_ok=True)
        config = Config(args.upstream, Recorder(run_dir), args.timeout)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        print(f"proxy: cannot start: {error}", file=sys.stderr)
        return 1

    server.daemon_threads = True
    server.config = config

    print(f"proxy: listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    print(f"proxy: recording to {run_dir}, forwarding to {args.upstream}", flush=True)

    install_shutdown(server)
    server.serve_forever()
    server.server_close()
    print(f"proxy: {config.recorder.count} calls in {run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
