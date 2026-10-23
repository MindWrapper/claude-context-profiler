"""The launcher: runs claude and records the session.

    claude-context-profiler [claude args...]

See AGENTS.md, sections "Command line", "Layout on disk" and "Parts".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import profiler

DEFAULT_UPSTREAM = "https://api.anthropic.com"
# These send the requests to another cloud and ignore ANTHROPIC_BASE_URL.
OTHER_CLOUDS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# A fresh machine scans Python on its first start (Gatekeeper, Windows Defender), which can take
# more than 10s. A child that fails exits at once, so the long wait only applies when it is slow.
START_TIMEOUT = 30.0
WINDOWS = os.name == "nt"


class SetupError(Exception):
    """Recording cannot start. The message is one line for the user."""


def say(message: str) -> None:
    print(f"ccprof: {message}", file=sys.stderr, flush=True)


def home_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONTEXT_PROFILER_HOME") or "~/.claude-context-profiler").expanduser().absolute()


# --- children ------------------------------------------------------------


def start_child(module: str, args: list[str], log: Path) -> subprocess.Popen:
    """Start a claude-context-profiler module in the background, out of reach of the terminal's Ctrl-C."""
    if WINDOWS:
        detach = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        detach = {"start_new_session": True}
    with log.open("w", encoding="utf-8") as out:
        return subprocess.Popen(
            [sys.executable, "-u", "-m", module, *args],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            **detach,
        )


def wait_for_port(child: subprocess.Popen, log: Path, pattern: str, what: str) -> int:
    """The port from the line the child prints once it listens."""
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8", errors="replace")
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
        if child.poll() is not None:
            lines = text.strip().splitlines()
            raise SetupError(f"the {what} did not start: {lines[-1] if lines else 'no output'}")
        time.sleep(0.05)
    # The run directory, and the log with it, is removed after this, so its last line goes in the message.
    lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    raise SetupError(f"the {what} did not start within {START_TIMEOUT:.0f}s" + (f": {lines[-1]}" if lines else ""))


def stop_child(child: subprocess.Popen | None, timeout: float) -> None:
    """Ask first, so the child finishes its file; insist later."""
    if child is None or child.poll() is not None:
        return
    try:
        if WINDOWS:
            # terminate() would kill the viewer before it writes the final page.
            os.kill(child.pid, signal.CTRL_BREAK_EVENT)
        else:
            child.terminate()
        child.wait(timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    except OSError:
        pass


# --- the session ---------------------------------------------------------


def run_claude(command: list[str], env: dict[str, str]) -> int:
    """Run claude in the foreground and return its exit code.

    claude owns the terminal and handles Ctrl-C itself. The launcher shares its
    process group, so it gets the same SIGINT and must neither die of it nor
    pass it on. A Python handler, unlike SIG_IGN, is reset to the default in
    the child, so claude still sees Ctrl-C as usual. SIGTERM and SIGHUP go on to
    claude, so the launcher outlives it and can clean up.
    """
    child: subprocess.Popen | None = None

    def forward(signum, frame):  # noqa: ARG001 - signal handler signature
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    names = ["SIGINT"] if WINDOWS else ["SIGINT", "SIGTERM", "SIGHUP"]
    saved = {name: signal.getsignal(getattr(signal, name)) for name in names}
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    for name in names[1:]:
        signal.signal(getattr(signal, name), forward)
    try:
        child = subprocess.Popen(command, env=env)
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                continue
    finally:
        for name, handler in saved.items():
            signal.signal(getattr(signal, name), handler)
    # Killed by a signal: report it the way a shell would.
    return 128 - code if code < 0 else code


def other_cloud() -> str | None:
    for name in OTHER_CLOUDS:
        if os.environ.get(name, "").strip().lower() not in ("", "0", "false"):
            return name
    return None


def make_run_dir(home: Path) -> Path:
    parent = home / "recordings" / Path.cwd().name
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parent.mkdir(parents=True, exist_ok=True)
    run_dir, n = parent / stamp, 1
    while True:
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            n += 1
            run_dir = parent / f"{stamp}-{n}"


class Recording:
    """The proxy and the viewer of one recorded session."""

    def __init__(self) -> None:
        self.run_dir: Path | None = None
        self.page_url: str | None = None
        self.proxy: subprocess.Popen | None = None
        self.viewer: subprocess.Popen | None = None

    def start(self, home: Path, upstream: str) -> str:
        """Start both children and return the base URL for claude."""
        try:
            self.run_dir = make_run_dir(home)
            meta = {
                "project": str(Path.cwd().absolute()),
                "upstream": upstream,
                "started": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            (self.run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

            run = str(self.run_dir)
            proxy_log = self.run_dir / "proxy.log"
            self.proxy = start_child("claude_context_profiler.proxy", [run, "--port", "0", "--upstream", upstream], proxy_log)
            port = wait_for_port(self.proxy, proxy_log, r"proxy: listening on http://127\.0\.0\.1:(\d+)", "proxy")

            viewer_log = self.run_dir / "viewer.log"
            viewer_args = [run, "--port", "0"]
            if os.environ.get("CLAUDE_CONTEXT_PROFILER_OPEN", "1") != "0":
                viewer_args.append("--open")
            say(f"recording to {self.run_dir}")
            self.start_viewer(viewer_args, viewer_log)
        except OSError as error:
            raise SetupError(f"cannot set up the recording: {error}") from error
        return f"http://127.0.0.1:{port}"

    def start_viewer(self, args: list[str], log: Path) -> None:
        """Best effort: without the viewer the recording goes on, and the page
        is built when the session ends."""
        try:
            self.viewer = start_child("claude_context_profiler.serve", args, log)
            page_port = wait_for_port(self.viewer, log, r"viewer: http://127\.0\.0\.1:(\d+)/", "viewer")
        except (OSError, SetupError) as error:
            stop_child(self.viewer, 5)
            self.viewer = None
            say(f"{error}; the live page is off, the page is built when the session ends")
            return
        self.page_url = f"http://127.0.0.1:{page_port}/"
        say(f"live page at {self.page_url}")

    def stop(self) -> None:
        # The proxy goes first, so that no call lands after the viewer writes
        # the final page.
        stop_child(self.proxy, 5)
        stop_child(self.viewer, 30)

    def discard(self) -> None:
        """Remove the run directory of a recording that never started."""
        if self.run_dir is not None:
            shutil.rmtree(self.run_dir, ignore_errors=True)
            self.run_dir = None

    def report(self) -> None:
        if self.run_dir is None:
            return
        page = self.run_dir / profiler.PAGE_NAME
        if not page.exists():
            try:
                profiler.build(self.run_dir)
            except ValueError:
                say(f"no API call recorded in {self.run_dir}")
                return
            except Exception as error:  # noqa: BLE001 - one line, not a traceback
                say(f"cannot build the page: {error}")
                return
        say(f"page: {page}")


def with_page_address(command: list[str], recording: Recording) -> list[str]:
    """Pass claude a SessionStart hook that shows the live page's address on its start screen.

    The terminal line that says it is gone once claude takes over the terminal. The settings file
    sits in the run directory and holds only the hook, so the user's own settings stay as they are.
    A user who passes --settings keeps it, and the address stays in the terminal line only.
    """
    if recording.page_url is None or recording.run_dir is None or "--settings" in command[1:]:
        return command
    python = f'"{sys.executable}"' if " " in sys.executable else sys.executable
    hook = {"type": "command", "command": f"{python} -m claude_context_profiler.hook {recording.page_url}"}
    path = recording.run_dir / "claude-settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}, indent=2) + "\n", encoding="utf-8")
    return [command[0], "--settings", str(path), *command[1:]]


def run_unrecorded(command: list[str], env: dict[str, str], reason: str) -> int:
    """Run claude without recording. The reason is said again when claude exits, because claude
    takes over the terminal and the first line easily goes unseen."""
    say(f"{reason}; running claude without recording")
    code = run_claude(command, env)
    say(f"this session was not recorded: {reason}")
    return code


def record_session(command: list[str], env: dict[str, str]) -> int:
    cloud = other_cloud()
    if cloud:
        return run_unrecorded(command, env, f"{cloud} is set, so the requests bypass any base URL")

    upstream = os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_UPSTREAM
    recording = Recording()
    try:
        env["ANTHROPIC_BASE_URL"] = recording.start(home_dir(), upstream)
    except SetupError as error:
        recording.stop()
        recording.discard()
        return run_unrecorded(command, env, str(error))
    except BaseException:
        recording.stop()
        recording.discard()
        raise

    try:
        return run_claude(with_page_address(command, recording), env)
    finally:
        recording.stop()
        recording.report()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = dict(os.environ)
    claude = shutil.which("claude")
    if not claude:
        say("claude is not on PATH. See https://code.claude.com/docs/en/setup")
        return 127
    command = [claude, *argv]

    try:
        return record_session(command, env)
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        say(f"cannot run claude: {error}")
        return 126


if __name__ == "__main__":
    sys.exit(main())
