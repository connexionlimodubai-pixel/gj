"""The desktop app (desktop.py): launching, one app per user, the window and its fallbacks, logging,
and what it changes elsewhere: config's base URL, the Connect Claude page and links that leave the window.

No test needs a display: pywebview is replaced by a fake module (or made unimportable).
"""

from __future__ import annotations

import html as html_lib
import inspect
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openberry import __version__, cli, config, desktop, repo
from openberry.models import LeadIn, SignalIn
from openberry.web import app as web_app
from openberry.web import pages

# --------------------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """OPENBERRY_HOME in a temporary folder; the settings the app writes are restored afterwards."""
    path = tmp_path / "home"
    monkeypatch.setattr(config, "OPENBERRY_HOME", path)
    for key in ("OPENBERRY_BASE_URL", "OPENBERRY_SECRET_KEY", "OPENBERRY_DB"):
        monkeypatch.setenv(key, "")  # recorded, so the value the app sets is undone too
        monkeypatch.delenv(key)
    return path


@pytest.fixture
def app_env(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """What `desktop.run` needs in a test: no scheduler, pytest's logging left alone, and no GUI
    (tests that want a window install FakeWebview)."""
    monkeypatch.setenv("OPENBERRY_SCHEDULER", "false")
    monkeypatch.setattr(desktop, "configure_logging", lambda home: None)
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` raises ImportError
    return home


@pytest.fixture
def root_logging() -> Iterator[None]:
    """Remove the handlers configure_logging adds to the root logger."""
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for handler in root.handlers[:]:
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def read_state(home: Path) -> dict[str, Any] | None:
    path = home / config.DESKTOP_STATE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def healthz(url: str) -> Any:
    return httpx.get(f"{url}/healthz", timeout=5, trust_env=False).json()


def assert_stopped(url: str) -> None:
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{url}/healthz", timeout=2, trust_env=False)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """Stands in for webbrowser.open: records what it opened and what the app looked like then,
    and sets `stop` as if the person pressed Ctrl+C afterwards."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.stop = threading.Event()
        self.opened: list[str] = []
        self.state: dict[str, Any] | None = None
        self.healthz: Any = None

    def __call__(self, url: str, *args: Any, **kwargs: Any) -> bool:
        self.opened.append(url)
        self.state = read_state(self.home)
        self.healthz = healthz(url)
        self.stop.set()
        return True


@pytest.fixture
def browser(home: Path, monkeypatch: pytest.MonkeyPatch) -> Browser:
    fake = Browser(home)
    monkeypatch.setattr(webbrowser, "open", fake)
    return fake


class FakeEvent:
    """pywebview's window event: handlers are added with +=; `closing` runs them on the GUI thread."""

    def __init__(self) -> None:
        self.handlers: list[Callable[[], Any]] = []

    def __iadd__(self, handler: Callable[[], Any]) -> FakeEvent:
        self.handlers.append(handler)
        return self

    def set(self) -> None:
        for handler in self.handlers:
            handler()


class FakeWindow:
    def __init__(self, title: str, url: str | None, kwargs: dict[str, Any]) -> None:
        self.title, self.url, self.kwargs = title, url, kwargs
        self.destroyed = False
        self.events = types.SimpleNamespace(shown=threading.Event(), closing=FakeEvent())
        self.events.shown.set()

    def destroy(self) -> None:
        self.destroyed = True


class FakeWebview(types.ModuleType):
    """pywebview's API as desktop.py uses it. `settings` refuses unknown keys, as pywebview's does."""

    class Settings(dict):
        def __setitem__(self, key: str, value: Any) -> None:
            if key not in self:
                raise KeyError(key)
            super().__setitem__(key, value)

    def __init__(self, renderer: str = "gtk", fail: BaseException | None = None,
                 while_open: Callable[[FakeWindow], None] | None = None) -> None:
        super().__init__("webview")
        self.settings = self.Settings(ALLOW_DOWNLOADS=False, OPEN_EXTERNAL_LINKS_IN_BROWSER=False)
        self.renderer, self.fail, self.while_open = renderer, fail, while_open
        self.windows: list[FakeWindow] = []
        self.started: list[dict[str, Any]] = []

    def create_window(self, title: str, url: str | None = None, **kwargs: Any) -> FakeWindow:
        window = FakeWindow(title, url, kwargs)
        self.windows.append(window)
        return window

    def start(self, func: Callable[..., None] | None = None, args: Any = None, **kwargs: Any) -> None:
        """Blocks while the window is open, like the real one; returns when it is closed."""
        self.started.append(kwargs)
        if self.fail is not None:
            raise self.fail
        if func is not None:
            func(*args)
        if self.while_open is not None:
            self.while_open(self.windows[-1])
        self.windows[-1].events.closing.set()  # the person closes the window


HEALTHY = {"ok": True, "app": "openberry"}  # OpenBerry's /healthz
HEALTHY_BODY = json.dumps(HEALTHY).encode()


class _Healthz(BaseHTTPRequestHandler):
    reply = HEALTHY_BODY

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        found = self.path == "/healthz"
        body = self.reply if found else b"{}"
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def fake_server() -> Iterator[Callable[[bytes], str]]:
    """Start a tiny server whose /healthz answers with the given body; returns its URL."""
    servers: list[ThreadingHTTPServer] = []

    def start(reply: bytes = HEALTHY_BODY) -> str:
        handler = type("Handler", (_Healthz,), {"reply": reply})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def lock_holder(home: Path) -> Iterator[desktop.InstanceLock]:
    """The lock of another app running for the same OPENBERRY_HOME."""
    holder = desktop.InstanceLock(home / desktop.LOCK_FILE)
    assert holder.acquire()
    yield holder
    holder.release()


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["serve", "--port", "9000"], ["mcp"], ["scan", "--company", "1"], ["demo"], ["rescore"], ["init-db"],
    ["--version"], ["-h"], ["--help"], ["desktop", "--smoke"],
])
def test_the_app_executable_also_runs_cli_commands(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "main", calls.append)
    monkeypatch.setattr(desktop, "run", lambda *a, **k: pytest.fail("the app must not start"))
    desktop.main(argv)
    assert calls == [argv]


def test_other_arguments_start_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[Any] = []
    exits: list[tuple[float, int]] = []
    monkeypatch.setattr(cli, "main", lambda argv: pytest.fail("not a CLI command"))
    monkeypatch.setattr(desktop, "run", lambda args, stop=None: runs.append(args) or 0)
    monkeypatch.setattr(desktop, "exit_after", lambda grace, code: exits.append((grace, code)))
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # the packaged app: freeze_support runs first

    desktop.main(["--port", "8123", "--no-window", "-psn_0_4242"])  # macOS Finder may add -psn_...
    args = runs[-1]
    assert (args.port, args.no_window, args.smoke, args.ignored) == (8123, True, False, ["-psn_0_4242"])

    monkeypatch.setattr(sys, "argv", ["OpenBerry"])  # a double-click: no arguments at all
    desktop.main()
    assert (runs[-1].port, runs[-1].no_window, runs[-1].smoke) == (None, False, False)

    monkeypatch.setattr(desktop, "run", lambda args, stop=None: 1)
    with pytest.raises(SystemExit) as exc:
        desktop.main([])
    assert exc.value.code == 1
    # The packaged app makes sure its process ends once the app is done (see exit_after).
    assert exits == [(desktop.EXIT_GRACE, 0), (desktop.EXIT_GRACE, 0), (desktop.EXIT_GRACE, 1)]

    monkeypatch.setattr(sys, "frozen", False)  # `openberry-desktop` from a Python install: exits normally
    monkeypatch.setattr(desktop, "run", lambda args, stop=None: 0)
    desktop.main([])
    assert len(exits) == 3


def test_exit_after_ends_a_process_that_lingers(monkeypatch: pytest.MonkeyPatch) -> None:
    exited = threading.Event()
    codes: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: codes.append(code) or exited.set())
    timer = desktop.exit_after(0.05, 3)
    assert timer.daemon  # never keeps the process alive itself
    assert exited.wait(5) and codes == [3]


def test_a_finished_app_process_really_exits(tmp_path: Path) -> None:
    """A thread that never ends (a stuck GUI or .NET thread) must not keep a closed app's process,
    and with it desktop.lock, alive."""
    script = ("import sys, threading, time; from openberry import desktop; "
              "threading.Thread(target=time.sleep, args=(600,)).start(); "  # not a daemon: blocks a normal exit
              "desktop.exit_after(0.2, 0); sys.exit(0)")
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert proc.returncode == 0 and time.monotonic() - started < 30


def test_cli_commands_in_the_windowed_exe_get_working_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The windowed executable has no console: sys.stdin/stdout/stderr are None. uvicorn's default log
    setup (`OpenBerry serve`) calls sys.stdout.isatty() and the MCP server reads sys.stdin."""
    import uvicorn.logging

    seen: dict[str, Any] = {}

    def fake_cli(argv: list[str]) -> None:
        uvicorn.logging.DefaultFormatter()  # isatty() on sys.stderr / sys.stdout
        uvicorn.logging.AccessFormatter()
        print("printed into nothing")
        seen["stdin"] = sys.stdin.read()
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            stream.close()

    monkeypatch.setattr(cli, "main", fake_cli)
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, None)
    desktop.main(["serve"])
    assert seen == {"stdin": ""}


@pytest.mark.parametrize("port", ["70000", "-1", "eighty"])
def test_bad_ports_are_refused_before_anything_starts(port: str) -> None:
    with pytest.raises(SystemExit):
        desktop.parse_args(["--port", port])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["desktop", "--port", port])


def test_openberry_desktop_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    args = cli.build_parser().parse_args(["desktop", "--port", "0", "--no-window"])
    assert args.func is cli._desktop and args.port == 0 and args.no_window and not args.smoke
    seen: list[Any] = []
    monkeypatch.setattr(desktop, "run", lambda args, stop=None: seen.append(args) or 1)
    with pytest.raises(SystemExit) as exc:
        cli.main(["desktop", "--smoke"])
    assert exc.value.code == 1 and seen[0].smoke
    monkeypatch.setattr(desktop, "run", lambda args, stop=None: 0)
    cli.main(["desktop"])  # exit code 0: returns normally


# --------------------------------------------------------------------------------------
# Ports and the server thread
# --------------------------------------------------------------------------------------

def test_port_8000_busy_picks_the_next_free_one() -> None:
    blocker = socket.socket()
    try:
        blocker.bind((desktop.HOST, desktop.DEFAULT_PORTS[0]))
        blocker.listen()
    except OSError:
        pass  # something on this machine already uses 8000: just as busy
    try:
        sock = desktop.bind_port(desktop.DEFAULT_PORTS)
        try:
            port = sock.getsockname()[1]
            assert port != 8000 and port in desktop.DEFAULT_PORTS
            assert desktop.socket_url(sock) == f"http://127.0.0.1:{port}"
        finally:
            sock.close()
    finally:
        blocker.close()


def test_busy_ports_are_skipped_and_reported() -> None:
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    busy_port, spare = busy.getsockname()[1], free_port()
    try:
        sock = desktop.bind_port([busy_port, spare])
        assert sock.getsockname()[1] == spare
        sock.close()
        with pytest.raises(desktop.DesktopError, match=f"Port {busy_port} is already in use"):
            desktop.bind_port([busy_port])
        with pytest.raises(desktop.DesktopError, match=f"No free port between {busy_port} and {busy_port}"):
            desktop.bind_port([busy_port, busy_port])
    finally:
        busy.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows binds exclusively instead of probing")
def test_a_port_another_program_answers_on_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS lets 127.0.0.1:<port> bind over another program's 0.0.0.0:<port>: the probe catches it."""
    taken, spare = free_port(), free_port()
    monkeypatch.setattr(desktop, "_port_answers", lambda host, port: port == taken)
    sock = desktop.bind_port([taken, spare])
    assert sock.getsockname()[1] == spare
    sock.close()


def test_server_thread_starts_answers_and_stops(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBERRY_SCHEDULER", "false")
    sock = desktop.bind_port([0])
    url = desktop.socket_url(sock)
    settings = desktop.desktop_settings(url, home)
    assert settings.base_url == url == os.environ["OPENBERRY_BASE_URL"]
    assert settings.db_path == home / "openberry.db"
    server = desktop.start_server(settings, sock)
    try:
        assert server.url == url and server.running()
        assert healthz(url) == HEALTHY
        assert "OpenBerry" in httpx.get(url, timeout=5, trust_env=False, follow_redirects=True).text
    finally:
        server.stop()
    assert not server.running()
    assert_stopped(url)


def _app_with_startup(startup: Callable[[], Any]) -> Callable[[Any], FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await startup()
        yield

    return lambda settings: FastAPI(lifespan=lifespan)


def test_a_server_that_fails_to_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(web_app, "create_app", _app_with_startup(broken))
    sock = desktop.bind_port([0])
    with pytest.raises(desktop.DesktopError, match="stopped while starting"):
        desktop.start_server(config.Settings(), sock)
    assert sock.fileno() == -1  # closed


def test_a_server_that_starts_too_slowly_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow() -> None:
        import asyncio

        await asyncio.sleep(0.6)

    monkeypatch.setattr(web_app, "create_app", _app_with_startup(slow))
    with pytest.raises(desktop.DesktopError, match="did not start within"):
        desktop.start_server(config.Settings(), desktop.bind_port([0]), timeout=0.1)
    for thread in threading.enumerate():
        if thread.name == "openberry-server":
            thread.join(5)
            assert not thread.is_alive()  # it gave up once its startup finished


# --------------------------------------------------------------------------------------
# One app per user: the lock and desktop.json
# --------------------------------------------------------------------------------------

def test_instance_lock_is_exclusive_until_released(home: Path) -> None:
    first = desktop.InstanceLock(home / desktop.LOCK_FILE)
    second = desktop.InstanceLock(home / desktop.LOCK_FILE)
    assert first.acquire() and first.held
    assert first.acquire()  # already held: still true
    assert not second.acquire() and not second.held
    first.release()
    assert second.acquire()
    second.release()


def test_instance_lock_is_freed_when_its_app_dies(home: Path) -> None:
    code = ("import sys, time; from pathlib import Path; from openberry.desktop import InstanceLock; "
            "lock = InstanceLock(Path(sys.argv[1])); print(lock.acquire(), flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(home / desktop.LOCK_FILE)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "True"
        assert not desktop.InstanceLock(home / desktop.LOCK_FILE).acquire()
    finally:
        proc.kill()  # a crash: no clean-up code runs
        proc.wait(10)
        if proc.stdout is not None:
            proc.stdout.close()
    lock = desktop.InstanceLock(home / desktop.LOCK_FILE)
    deadline = time.monotonic() + 5  # Windows may take a moment to drop a dead process's lock
    while not lock.acquire():
        assert time.monotonic() < deadline, "the lock outlived its process"
        time.sleep(0.1)
    lock.release()


def test_desktop_json_is_replaced_atomically_and_only_removed_by_its_owner(home: Path) -> None:
    desktop.write_state(home, "http://127.0.0.1:8001")
    assert read_state(home) == {"url": "http://127.0.0.1:8001", "pid": os.getpid(), "version": __version__}
    assert [p.name for p in home.iterdir()] == [config.DESKTOP_STATE_FILE]  # no temporary file left
    desktop.remove_state(home, "http://127.0.0.1:8002")  # another URL: a newer app's file
    assert read_state(home) is not None
    (home / config.DESKTOP_STATE_FILE).write_text(json.dumps({"url": "http://127.0.0.1:8001", "pid": -5}))
    desktop.remove_state(home, "http://127.0.0.1:8001")  # same URL, another process
    assert read_state(home) is not None
    desktop.write_state(home, "http://127.0.0.1:8001")
    desktop.remove_state(home, "http://127.0.0.1:8001")
    assert read_state(home) is None
    desktop.remove_state(home, "http://127.0.0.1:8001")  # already gone: fine


@pytest.mark.parametrize(("url", "local"), [
    ("http://127.0.0.1:8000", True), ("http://localhost:8123", True), ("http://[::1]:8000", True),
    ("http://127.0.0.1", False), ("https://127.0.0.1:8000", False), ("http://example.com:8000", False),
    ("http://10.0.0.5:8000", False), ("http://[::1", False), ("", False),
])
def test_only_local_addresses_are_opened_from_desktop_json(url: str, local: bool) -> None:
    assert desktop.is_local_url(url) is local


def test_healthz_probe_recognises_openberry(fake_server: Callable[[bytes], str]) -> None:
    assert desktop.answers_like_openberry(fake_server(HEALTHY_BODY))
    assert not desktop.answers_like_openberry(fake_server(b'{"ok": true}'))  # another program's health check
    assert not desktop.answers_like_openberry(fake_server(b'{"ok": "yes", "app": "openberry"}'))
    assert not desktop.answers_like_openberry(fake_server(b'{"status": "ok"}'))  # some other program
    assert not desktop.answers_like_openberry(fake_server(b"not json"))
    assert not desktop.answers_like_openberry(f"http://127.0.0.1:{free_port()}", timeout=1)  # nobody


def test_running_instance_reads_desktop_json(home: Path, fake_server: Callable[[bytes], str]) -> None:
    assert desktop.running_instance(home) is None  # no desktop.json
    url = fake_server(HEALTHY_BODY)
    desktop.write_state(home, url)
    assert desktop.running_instance(home) is None  # no app holds the lock: a file left behind by a crash
    holder = desktop.InstanceLock(home / desktop.LOCK_FILE)
    assert holder.acquire()
    try:
        assert desktop.running_instance(home) == url
        desktop.write_state(home, f"http://127.0.0.1:{free_port()}")  # the app is still starting, or hangs
        assert desktop.running_instance(home) is None
        desktop.write_state(home, fake_server(b'{"ok": true}'))  # another program took the port
        assert desktop.running_instance(home) is None
    finally:
        holder.release()


def test_app_is_running_follows_the_lock(home: Path) -> None:
    assert desktop.app_is_running(home) is False  # no app has ever run here
    holder = desktop.InstanceLock(home / desktop.LOCK_FILE)
    assert holder.acquire()
    assert desktop.app_is_running(home) is True
    holder.release()
    assert desktop.app_is_running(home) is False
    assert holder.acquire()  # the probe left the lock free for the next app
    holder.release()


def test_a_dead_apps_desktop_json_is_ignored_everywhere(home: Path) -> None:
    """Cmd+Q on macOS, End task, a crash or a power cut leave desktop.json behind; nothing may trust it."""
    script = ("import sys, time; from pathlib import Path; from openberry import desktop; "
              "home = Path(sys.argv[1]); lock = desktop.InstanceLock(home / desktop.LOCK_FILE); "
              "assert lock.acquire(); desktop.write_state(home, 'http://127.0.0.1:8042'); "
              "print('ready', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", script, str(home)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        assert config.desktop_url(home) == "http://127.0.0.1:8042"  # its app is running
        proc.kill()  # no clean-up runs, as after a crash
        proc.wait(10)
        assert read_state(home)["url"] == "http://127.0.0.1:8042"
        assert config.desktop_url(home) == ""
        assert config.Settings.from_env().base_url == config.DEFAULT_BASE_URL
    finally:
        proc.kill()
        proc.wait(10)
        if proc.stdout is not None:
            proc.stdout.close()


# --------------------------------------------------------------------------------------
# The app: run()
# --------------------------------------------------------------------------------------

def test_app_serves_writes_desktop_json_and_cleans_up(app_env: Path, browser: Browser,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    home = app_env
    home.mkdir()
    stale = {"url": "http://127.0.0.1:9", "pid": 999_999, "version": "0.0.1"}  # a crashed app's
    (home / config.DESKTOP_STATE_FILE).write_text(json.dumps(stale))
    # This process holds the lock, so no other app runs: the stale file isn't even probed.
    monkeypatch.setattr(desktop, "running_instance", lambda home: pytest.fail("probed desktop.json"))

    assert desktop.run(desktop.parse_args(["--no-window", "--port", "0"]), stop=browser.stop) == 0

    [url] = browser.opened
    assert browser.healthz == HEALTHY
    assert browser.state == {"url": url, "pid": os.getpid(), "version": __version__}
    assert os.environ["OPENBERRY_BASE_URL"] == url == config.get_settings().base_url
    assert read_state(home) is None
    assert_stopped(url)
    lock = desktop.InstanceLock(home / desktop.LOCK_FILE)
    assert lock.acquire()  # released
    lock.release()


def test_second_start_shows_the_running_app(app_env: Path, browser: Browser, lock_holder: desktop.InstanceLock,
                                            fake_server: Callable[[bytes], str],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    home = app_env
    other = fake_server(HEALTHY_BODY)
    (home / config.DESKTOP_STATE_FILE).write_text(json.dumps({"url": other, "pid": 1, "version": __version__}))
    monkeypatch.setattr(desktop, "start_server", lambda *a, **k: pytest.fail("started a second server"))

    assert desktop.run(desktop.parse_args(["--no-window"])) == 0
    assert browser.opened == [other]
    assert read_state(home)["url"] == other  # the running app's file is left alone

    fake = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert desktop.run(desktop.parse_args([])) == 0
    assert [w.url for w in fake.windows] == [other] and browser.opened == [other]


def test_start_while_another_app_quits_waits_then_serves(app_env: Path, browser: Browser,
                                                         lock_holder: desktop.InstanceLock) -> None:
    threading.Timer(0.4, lock_holder.release).start()
    started = time.monotonic()
    assert desktop.run(desktop.parse_args(["--no-window", "--port", "0"]), stop=browser.stop) == 0
    assert time.monotonic() - started >= 0.3
    assert len(browser.opened) == 1 and browser.healthz == HEALTHY


def test_an_app_that_never_answers_gives_a_clear_error(app_env: Path, lock_holder: desktop.InstanceLock,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "STARTUP_TIMEOUT", 0.3)
    shown: list[str] = []
    monkeypatch.setattr(desktop, "show_error", lambda message, log_file: shown.append(message))
    assert desktop.run(desktop.parse_args([])) == 1
    assert len(shown) == 1 and "already starting" in shown[0]
    assert desktop.run(desktop.parse_args(["--no-window"])) == 1  # browser mode: logged, no window
    assert len(shown) == 1


def test_window_shows_the_dashboard_and_closing_it_stops_the_server(app_env: Path, browser: Browser,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    home = app_env
    seen: dict[str, Any] = {}

    def while_open(window: FakeWindow) -> None:
        seen["state"], seen["healthz"] = read_state(home), healthz(window.url)

    fake = FakeWebview(while_open=while_open)
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert desktop.run(desktop.parse_args(["--port", "0"])) == 0

    [window] = fake.windows
    assert window.title == "OpenBerry"
    assert window.kwargs == {"width": 1280, "height": 860, "min_size": (960, 640)}
    assert fake.settings == {"ALLOW_DOWNLOADS": True, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}
    assert fake.started[0]["private_mode"] is False  # the login cookie survives a restart
    assert fake.started[0]["storage_path"] == str(home / "webview")
    assert seen == {"state": {"url": window.url, "pid": os.getpid(), "version": __version__},
                    "healthz": HEALTHY}
    assert not window.destroyed and not browser.opened
    assert read_state(home) is None  # the window closed: everything is cleaned up
    assert_stopped(window.url)


def test_no_pywebview_falls_back_to_the_browser(app_env: Path, browser: Browser) -> None:
    # app_env makes `import webview` fail; the run must not block once the browser has opened.
    assert desktop.run(desktop.parse_args(["--port", "0"]), stop=browser.stop) == 0
    assert len(browser.opened) == 1 and browser.healthz == HEALTHY


@pytest.mark.parametrize("fake", [
    FakeWebview(fail=RuntimeError("You must have either QT or GTK with Python extensions installed")),
    FakeWebview(renderer="mshtml"),  # Windows without the WebView2 runtime: Internet Explorer can't run it
], ids=["no-gui-toolkit", "internet-explorer"])
def test_a_window_that_cannot_open_falls_back_to_the_browser(app_env: Path, browser: Browser, fake: FakeWebview,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fake.windows.clear()
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert desktop.run(desktop.parse_args(["--port", "0"]), stop=browser.stop) == 0
    assert len(browser.opened) == 1 and browser.healthz == HEALTHY
    assert fake.windows[0].destroyed is (fake.renderer == "mshtml")
    assert read_state(app_env) is None


def test_ctrl_c_with_the_window_open_still_cleans_up(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def interrupt(window: FakeWindow) -> None:
        urls.append(window.url)
        raise KeyboardInterrupt

    monkeypatch.setitem(sys.modules, "webview", FakeWebview(while_open=interrupt))
    assert desktop.run(desktop.parse_args(["--port", "0"])) == 130
    assert read_state(app_env) is None
    assert_stopped(urls[0])


def test_closing_the_window_forgets_the_server_at_once(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS ends the process when the app is quit (Cmd+Q, log out) without returning from
    webview.start(): desktop.json must be gone by the time the window's closing event is over."""
    home = app_env
    seen: dict[str, Any] = {}

    class QuitOnClose(FakeWebview):
        def start(self, func: Callable[..., None] | None = None, args: Any = None, **kwargs: Any) -> None:
            window = self.windows[-1]
            seen["before"] = read_state(home)
            window.events.closing.set()  # Cmd+Q: the closing event, then the process ends
            seen["after"] = read_state(home)
            seen["serving"] = healthz(window.url)

    fake = QuitOnClose()
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert desktop.run(desktop.parse_args(["--port", "0"])) == 0
    assert seen["before"]["url"] == fake.windows[0].url and seen["before"]["pid"] == os.getpid()
    assert seen["after"] is None and seen["serving"] == HEALTHY


def test_a_second_window_does_not_touch_the_running_apps_file(app_env: Path, lock_holder: desktop.InstanceLock,
                                                              fake_server: Callable[[bytes], str],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    other = fake_server(HEALTHY_BODY)
    desktop.write_state(app_env, other)
    fake = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert desktop.run(desktop.parse_args([])) == 0
    assert [w.url for w in fake.windows] == [other] and not fake.windows[0].events.closing.handlers
    assert read_state(app_env)["url"] == other


def test_unexpected_errors_are_reported_not_raised(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(desktop, "bind_port", broken)
    shown: list[str] = []
    monkeypatch.setattr(desktop, "show_error", lambda message, log_file: shown.append(message))
    assert desktop.run(desktop.parse_args([])) == 1
    assert shown == ["Unexpected error: boom"]


def test_session_key_and_address(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = desktop.persistent_secret_key(home)
    assert len(key) == 64 and desktop.persistent_secret_key(home) == key  # the same after a restart
    if os.name != "nt":
        assert (home / desktop.SECRET_KEY_FILE).stat().st_mode & 0o777 == 0o600
    settings = desktop.desktop_settings("http://127.0.0.1:8123", home)
    assert (settings.secret_key, settings.base_url) == (key, "http://127.0.0.1:8123")
    # The person's own settings win.
    monkeypatch.setenv("OPENBERRY_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("OPENBERRY_BASE_URL", "https://leads.example.com")
    settings = desktop.desktop_settings("http://127.0.0.1:8124", home)
    assert (settings.secret_key, settings.base_url) == ("k" * 40, "https://leads.example.com")
    assert os.environ["OPENBERRY_BASE_URL"] == "https://leads.example.com"


# --------------------------------------------------------------------------------------
# --smoke: CI checks a packaged app without a display
# --------------------------------------------------------------------------------------

def test_smoke_checks_a_real_server_and_stops(app_env: Path, lock_holder: desktop.InstanceLock,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    # Another app is running (it holds the lock): the check neither waits for it nor disturbs it.
    assert desktop.run(desktop.parse_args(["--smoke", "--port", "0"])) == 0
    out = capsys.readouterr().out
    match = re.search(r"OpenBerry \S+ smoke test passed at (http://127\.0\.0\.1:\d+)", out)
    assert match, out
    assert_stopped(match.group(1))
    assert read_state(app_env) is None
    assert config.get_settings().scheduler_enabled is False


def test_smoke_failure_exits_1(app_env: Path, monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(desktop, "smoke_problems", lambda url: ["GET /static/app.css answered 404"])
    assert desktop.run(desktop.parse_args(["--smoke", "--port", "0"])) == 1
    assert "smoke test FAILED" in capsys.readouterr().err


def test_smoke_from_the_command_line(tmp_path: Path) -> None:
    """`python -m openberry desktop --smoke`, as CI runs the packaged app: a fresh process and home."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENBERRY_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), OPENBERRY_ENV_FILE=str(tmp_path / "none.env"))
    proc = subprocess.run([sys.executable, "-m", "openberry", "desktop", "--smoke", "--port", "0"], cwd=tmp_path,
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "smoke test passed at http://127.0.0.1:" in proc.stdout
    assert (tmp_path / ".openberry" / "openberry.db").is_file()
    assert not (tmp_path / ".openberry" / config.DESKTOP_STATE_FILE).exists()


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

def test_packaged_app_logs_to_a_small_rotating_file(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                    root_logging: None) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    log_file = desktop.configure_logging(home)
    assert log_file == home / "logs" / "desktop.log"
    logging.getLogger("openberry.desktop").info("hello from the app")
    [handler] = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    handler.flush()
    assert handler.maxBytes == desktop.LOG_FILE_BYTES and handler.backupCount == desktop.LOG_FILE_BACKUPS
    assert "hello from the app" in log_file.read_text(encoding="utf-8")


def test_without_a_console_nothing_is_written_to_none(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                      root_logging: None) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log_file = desktop.configure_logging(home)
    assert log_file is not None
    assert not [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]
    logging.getLogger("openberry.desktop").warning("still logged")
    desktop._ensure_std_streams()
    try:
        print("goes nowhere")
        sys.stderr.write("nor this\n")
    finally:
        sys.stdout.close()
        sys.stderr.close()


def test_a_console_run_logs_to_the_console_only(home: Path, root_logging: None) -> None:
    assert desktop.configure_logging(home) is None
    assert not (home / "logs").exists()


# --------------------------------------------------------------------------------------
# config: `openberry mcp` (started by Claude Desktop) links to the running app
# --------------------------------------------------------------------------------------

def test_base_url_falls_back_to_the_running_desktop_app(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                        lock_holder: desktop.InstanceLock) -> None:
    assert config.Settings.from_env().base_url == config.DEFAULT_BASE_URL
    desktop.write_state(home, "http://127.0.0.1:8042")
    assert config.Settings.from_env().base_url == "http://127.0.0.1:8042"
    monkeypatch.setenv("OPENBERRY_BASE_URL", "https://leads.example.com/")  # set by the person: wins
    assert config.Settings.from_env().base_url == "https://leads.example.com"


def test_mcp_links_follow_an_app_started_after_claude(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Desktop starts `openberry mcp` with Claude, often before OpenBerry: its settings are read
    then, but its links must use the port the app runs on now."""
    from openberry import mcp_server

    monkeypatch.setattr(config, "_settings", config.Settings.from_env())
    assert mcp_server._base_url() == config.DEFAULT_BASE_URL  # no app yet
    holder = desktop.InstanceLock(home / desktop.LOCK_FILE)
    assert holder.acquire()  # the app starts on another port
    desktop.write_state(home, "http://127.0.0.1:8042")
    try:
        assert mcp_server._base_url() == "http://127.0.0.1:8042"
        assert mcp_server.lead_url(1, 7) == "http://127.0.0.1:8042/c/1/leads/7"
        monkeypatch.setenv("OPENBERRY_BASE_URL", "https://leads.example.com")  # the person's own setting wins
        assert mcp_server._base_url() == config.get_settings().base_url == config.DEFAULT_BASE_URL
    finally:
        holder.release()
    monkeypatch.delenv("OPENBERRY_BASE_URL")
    assert mcp_server._base_url() == config.DEFAULT_BASE_URL  # the app quit (its file stays after a crash)


@pytest.mark.parametrize("content", [
    "not json", "[]", "{}", '{"url": 42}', '{"url": "ftp://127.0.0.1:8000"}', '{"url": "http://"}',
    '{"url": "http://[::1"}',
])
def test_an_unusable_desktop_json_is_ignored(home: Path, content: str) -> None:
    home.mkdir()
    (home / config.DESKTOP_STATE_FILE).write_text(content, encoding="utf-8")
    assert config.desktop_url(home) == ""
    assert config.Settings.from_env().base_url == config.DEFAULT_BASE_URL


# --------------------------------------------------------------------------------------
# Web: the Connect Claude page and links in the app window
# --------------------------------------------------------------------------------------

@pytest.fixture
def client(settings: config.Settings) -> Iterator[TestClient]:
    settings.scheduler_enabled = False
    settings.http_mcp_enabled = False
    settings.allowed_hosts = ["testserver"]
    with TestClient(web_app.create_app(settings)) as c:
        yield c


def _desktop_config(page: str) -> dict[str, Any]:
    match = re.search(r'<pre id="cfg-desktop">(.*?)</pre>', page, re.S)
    assert match
    return json.loads(html_lib.unescape(match.group(1)))["mcpServers"]["openberry"]


def test_packaged_app_tells_claude_to_run_its_own_cli(client: TestClient, settings: config.Settings,
                                                      monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    macos = tmp_path / "OpenBerry.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    app = macos / "OpenBerry"
    app.write_text("")
    db_path = str(settings.db_path.resolve())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app))
    monkeypatch.setattr(pages, "in_container", lambda: True)  # the packaged app wins even so

    # No console executable next to the app: the app's own executable runs CLI commands too.
    assert pages.mcp_launch(db_path) == {"command": str(app), "args": ["mcp"], "env": {"OPENBERRY_DB": db_path},
                                         "docker": False}
    (macos / "openberry-cli").write_text("")
    assert _desktop_config(client.get("/help").text) == {
        "command": str(macos / "openberry-cli"), "args": ["mcp"], "env": {"OPENBERRY_DB": db_path}}

    with monkeypatch.context() as m:  # Windows: openberry-cli.exe next to OpenBerry.exe
        m.setattr(sys, "executable", str(tmp_path / "OpenBerry.exe"))
        m.setattr(sys, "platform", "win32")
        (tmp_path / "openberry-cli.exe").write_text("")
        assert pages.bundled_cli() == str(tmp_path / "openberry-cli.exe")


def test_connect_claude_warns_when_the_app_runs_from_a_place_that_disappears(
        client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    monkeypatch.setattr(pages, "_temp_dir", lambda: str(temp))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def help_page_for(executable: Path) -> str:
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("")
        monkeypatch.setattr(sys, "executable", str(executable))
        page = client.get("/help").text
        assert _desktop_config(page)["command"] == str(executable)
        return page

    # macOS runs a downloaded app that wasn't moved from a random temporary copy (App Translocation).
    translocated = tmp_path / "AppTranslocation" / "5A1B" / "d" / "OpenBerry.app" / "Contents" / "MacOS" / "OpenBerry"
    assert pages.unstable_location(str(translocated)) == pages.MOVE_TO_APPLICATIONS
    assert "drag it into your Applications folder" in html_lib.unescape(help_page_for(translocated))
    # Windows runs an app opened inside its zip file from a temporary folder.
    in_zip = temp / "Temp1_OpenBerry-windows-x64.zip" / "OpenBerry" / "OpenBerry"
    assert pages.unstable_location(str(in_zip)) == pages.EXTRACT_THE_ZIP
    assert "Extract All" in help_page_for(in_zip)
    # Where it belongs: no warning, and the note talks about the app, not a virtual environment.
    installed = tmp_path / "Applications" / "OpenBerry.app" / "Contents" / "MacOS" / "OpenBerry"
    assert pages.unstable_location(str(installed)) == ""
    assert pages.unstable_location(str(tmp_path / "temporary" / "OpenBerry")) == ""  # only inside the temp folder
    page = help_page_for(installed)
    assert 'id="install-warning"' not in page and "Moved OpenBerry to another folder?" in page
    assert "virtual environment" not in page

    monkeypatch.setattr(sys, "frozen", False)  # a Python install: never warned about, its own wording
    page = client.get("/help").text
    assert 'id="install-warning"' not in page and "virtual environment" in page


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.append({k: v or "" for k, v in attrs})


def _links(page: str) -> list[dict[str, str]]:
    parser = _Links()
    parser.feed(page)
    return parser.links


def test_external_links_open_outside_the_app_window(client: TestClient, company: Any) -> None:
    """pywebview opens target=_blank links in the system browser; anything else would replace the
    dashboard in a window that has no Back button."""
    lead, _ = repo.upsert_lead(company.id, LeadIn(
        full_name="Nadia Karim", lead_company="Gulf Bank", linkedin_url="https://www.linkedin.com/in/nadia-karim",
        profile_url="https://profiles.example.org/nadia", website="https://gulfbank.example",
        email="nadia@gulfbank.example", github_username="nadiak",
        signals=[SignalIn(type="hiring", title="Hiring a Travel Manager", url="https://jobs.example.com/123")],
    ))
    base = f"/c/{company.id}"
    external: set[str] = set()
    for path in (f"{base}/leads/{lead.id}", f"{base}/signals", base, "/companies", "/help"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        for link in _links(resp.text):
            href = link.get("href", "")
            if urlsplit(href).scheme:  # off this server: LinkedIn, profiles, sources, docs, e-mail
                external.add(href)
                assert link.get("target") == "_blank", (path, href)
                assert {"noopener", "noreferrer"} <= set(link.get("rel", "").split()), (path, href)
            elif link.get("target") == "_blank":  # on this server: only raw documents leave the window
                assert href == "/api/openapi.json", (path, href)
    assert {"https://www.linkedin.com/in/nadia-karim", "https://profiles.example.org/nadia",
            "https://gulfbank.example", "mailto:nadia@gulfbank.example", "https://github.com/nadiak",
            "https://jobs.example.com/123", "https://acme.example"} <= external
    assert any(h.startswith("https://github.com/connexionlimodubai-pixel/gj") for h in external)  # docs


def test_csv_export_is_a_download_in_the_app_window(client: TestClient, company: Any) -> None:
    """WebKit (macOS) would show the CSV as text in the window instead of saving it without `download`."""
    [export] = [a for a in _links(client.get(f"/c/{company.id}/leads").text) if ".csv" in a.get("href", "")]
    assert "download" in export
    resp = client.get(export["href"])
    assert resp.status_code == 200 and resp.headers["content-disposition"].startswith("attachment;")


def test_pywebview_has_the_api_desktop_uses() -> None:
    """Checked against the installed pywebview, so an upgrade that renames these fails here, not on a laptop."""
    webview = pytest.importorskip("webview")
    for key in ("ALLOW_DOWNLOADS", "OPEN_EXTERNAL_LINKS_IN_BROWSER"):
        assert key in webview.settings
    start = inspect.signature(webview.start).parameters
    assert {"func", "args", "private_mode", "storage_path"} <= set(start)
    create = inspect.signature(webview.create_window).parameters
    assert {"title", "url", "html", "width", "height", "min_size"} <= set(create)
    # The closing event runs its handlers right away, on the closing thread: the only code that runs
    # when macOS quits the app. Creating a window opens nothing until webview.start().
    window = webview.create_window("probe", html="<p>probe</p>")
    try:
        closed: list[bool] = []
        window.events.closing += lambda: closed.append(True)
        assert window.events.closing.set() is False and closed == [True]  # handled, and not cancelled
    finally:
        webview.windows.remove(window)
