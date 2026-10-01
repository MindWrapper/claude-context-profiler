"""The launcher: runs claude, on a profile if asked, and records the session with -x.

    ccwrap [claude args...]
    ccwrap NAME [claude args...]
    ccwrap -x [NAME] [claude args...]
    ccwrap --ccwrap-help

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

HELP = """\
ccwrap launches Claude Code, on a named config profile if you ask for one, and can
record the session to show what every request to the model is made of.

usage:
  ccwrap [claude args...]            plain claude
  ccwrap NAME [claude args...]       claude with CLAUDE_CONFIG_DIR=$CCWRAP_HOME/profiles/NAME
  ccwrap -x [NAME] [claude args...]  the same, recorded, with the profiler page live in the browser
  ccwrap --ccwrap-help               this text

-x / --xray comes first. A bare word right after it, or a bare first word without it,
is a profile name; everything after that goes to claude unchanged. ccwrap never
creates a profile: record with `ccwrap -x`, tick what to drop in the page, and paste
the prompt it shows into Claude.

environment:
  CCWRAP_HOME   where profiles and recordings live (default ~/.ccwrap)
  CCWRAP_PORT   the proxy port; the viewer takes the next one (default: free ports)
  CCWRAP_OPEN   0 keeps the browser closed (default: open it at the first API call)

A recording goes to $CCWRAP_HOME/recordings/PROFILE/PROJECT/STAMP/; the page is
context_profiler.html in there.
"""

NAME_RE = re.compile(r"[A-Za-z0-9.][A-Za-z0-9._-]*")
DEFAULT_UPSTREAM = "https://api.anthropic.com"
# These send the requests to another cloud and ignore ANTHROPIC_BASE_URL.
OTHER_CLOUDS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
START_TIMEOUT = 10.0
WINDOWS = os.name == "nt"


class SetupError(Exception):
    """Recording cannot start. The message is one line for the user."""


def say(message: str) -> None:
    print(f"ccwrap: {message}", file=sys.stderr, flush=True)


def ccwrap_home() -> Path:
    return Path(os.environ.get("CCWRAP_HOME") or "~/.ccwrap").expanduser().absolute()


def is_profile_name(word: str) -> bool:
    return bool(NAME_RE.fullmatch(word)) and word not in (".", "..")


def parse(argv: list[str]) -> tuple[bool, str | None, list[str]]:
    """Split the command line into (record, profile, claude args)."""
    record = bool(argv) and argv[0] in ("-x", "--xray")
    if record:
        argv = argv[1:]
    # A word that cannot be a profile name, such as a prompt with spaces in it,
    # goes to claude: it can never be a typo of a profile.
    if argv and is_profile_name(argv[0]):
        return record, argv[0], argv[1:]
    return record, None, argv


def missing_profile(name: str, home: Path) -> str:
    profiles_dir = home / "profiles"
    names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else []
    existing = ", ".join(names) if names else "none yet"
    return (
        f"no profile '{name}' in {profiles_dir} (existing: {existing}). To make one, "
        "record with `ccwrap -x`, tick what to drop in the page, paste the prompt into Claude"
    )


# --- children ------------------------------------------------------------


def start_child(module: str, args: list[str], log: Path) -> subprocess.Popen:
    """Start a ccwrap module in the background, out of reach of the terminal's Ctrl-C."""
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
    raise SetupError(f"the {what} did not start within {START_TIMEOUT:.0f}s, see {log}")


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


def make_run_dir(home: Path, profile: str | None) -> Path:
    parent = home / "recordings" / (profile or "_default") / Path.cwd().name
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


def ports() -> tuple[int, int]:
    raw = os.environ.get("CCWRAP_PORT", "").strip()
    if not raw:
        return 0, 0
    if not raw.isdigit() or not 0 < int(raw) < 65535:
        raise SetupError(f"CCWRAP_PORT must be a port number, got '{raw}'")
    return int(raw), int(raw) + 1


class Recording:
    """The proxy and the viewer of one recorded session."""

    def __init__(self) -> None:
        self.run_dir: Path | None = None
        self.proxy: subprocess.Popen | None = None
        self.viewer: subprocess.Popen | None = None

    def start(self, home: Path, profile: str | None, config_dir: Path, upstream: str) -> str:
        """Start both children and return the base URL for claude."""
        proxy_port, viewer_port = ports()
        try:
            self.run_dir = make_run_dir(home, profile)
            meta = {
                "profile": profile,
                "config_dir": str(config_dir),
                "project": str(Path.cwd().absolute()),
                "upstream": upstream,
                "home": str(home),
                "started": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            (self.run_dir / "ccwrap.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

            run = str(self.run_dir)
            proxy_log = self.run_dir / "proxy.log"
            self.proxy = start_child(
                "ccwrap.proxy", [run, "--port", str(proxy_port), "--upstream", upstream], proxy_log
            )
            port = wait_for_port(self.proxy, proxy_log, r"proxy: listening on http://127\.0\.0\.1:(\d+)", "proxy")

            viewer_log = self.run_dir / "viewer.log"
            viewer_args = [run, "--port", str(viewer_port)]
            if os.environ.get("CCWRAP_OPEN", "1") != "0":
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
            self.viewer = start_child("ccwrap.serve", args, log)
            page_port = wait_for_port(self.viewer, log, r"viewer: http://127\.0\.0\.1:(\d+)/", "viewer")
        except (OSError, SetupError) as error:
            stop_child(self.viewer, 5)
            self.viewer = None
            say(f"{error}; the live page is off, the page is built when the session ends")
            return
        say(f"live page at http://127.0.0.1:{page_port}/")

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


def record_session(command: list[str], env: dict[str, str], profile: str | None, config_dir: Path) -> int:
    cloud = other_cloud()
    if cloud:
        say(f"{cloud} is set, so the requests bypass any base URL; running claude without recording")
        return run_claude(command, env)

    upstream = os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_UPSTREAM
    recording = Recording()
    try:
        env["ANTHROPIC_BASE_URL"] = recording.start(ccwrap_home(), profile, config_dir, upstream)
    except SetupError as error:
        recording.stop()
        recording.discard()
        say(f"{error}; running claude without recording")
        return run_claude(command, env)
    except BaseException:
        recording.stop()
        recording.discard()
        raise

    try:
        return run_claude(command, env)
    finally:
        recording.stop()
        recording.report()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--ccwrap-help"]:
        print(HELP, end="")
        return 0

    record, profile, claude_args = parse(argv)
    home = ccwrap_home()
    env = dict(os.environ)
    if profile:
        config_dir = home / "profiles" / profile
        if not config_dir.is_dir():
            say(missing_profile(profile, home))
            return 2
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    else:
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser().absolute()

    claude = shutil.which("claude")
    if not claude:
        say("claude is not on PATH. See https://code.claude.com/docs/en/setup")
        return 127
    command = [claude, *claude_args]

    try:
        if record:
            return record_session(command, env, profile, config_dir)
        return run_claude(command, env)
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        say(f"cannot run claude: {error}")
        return 126


if __name__ == "__main__":
    sys.exit(main())
