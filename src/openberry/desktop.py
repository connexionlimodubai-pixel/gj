"""The desktop app: OpenBerry in its own window, without a terminal.

`openberry desktop` and the packaged OpenBerry app (its executable runs `main`) start the dashboard
on 127.0.0.1 in a background thread and show it in a native window (pywebview). Without pywebview or
a GUI toolkit, or with --no-window, the default browser opens it instead and it serves until Ctrl+C.

One app per user: the running app writes OPENBERRY_HOME/desktop.json ({url, pid, version}), and
opening the app again shows that server instead of starting a second one. `openberry mcp`, which
Claude Desktop starts, reads the URL from it for its dashboard links (config.desktop_url).

The packaged executable doubles as the command line: `OpenBerry mcp` runs `openberry mcp`.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import logging
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import __version__, config
from .config import DESKTOP_STATE_FILE, Settings

if TYPE_CHECKING:
    import uvicorn

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORTS = range(8000, 8100)  # 8000 first, as `openberry serve`, so the README's address usually works
STARTUP_TIMEOUT = 30.0
STOP_TIMEOUT = 10.0
PROBE_TIMEOUT = 2.0
WINDOW_TITLE = "OpenBerry"
WINDOW_SIZE = (1280, 860)
MIN_WINDOW_SIZE = (960, 640)
LOG_FILE_BYTES = 1_000_000
LOG_FILE_BACKUPS = 2
SECRET_KEY_FILE = "desktop-secret-key"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# First arguments handed to the command line (cli.main), so the packaged app's executable also works
# as `openberry`: Claude Desktop can start `OpenBerry mcp`, and CI can run `OpenBerry desktop --smoke`.
CLI_COMMANDS = frozenset({"serve", "desktop", "mcp", "scan", "demo", "rescore", "init-db", "--version", "-h", "--help"})


class DesktopError(RuntimeError):
    """A problem the person can act on. The message is shown to them as is."""


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The desktop options, shared by `openberry desktop` and `openberry-desktop`."""
    parser.add_argument("--port", type=int, help=f"port on {HOST}; 0 picks any free one "
                                                 f"(default: {DEFAULT_PORTS[0]}, or the next free one up to {DEFAULT_PORTS[-1]})")
    parser.add_argument("--no-window", action="store_true", help="open the dashboard in your browser instead of a window")
    parser.add_argument("--smoke", action="store_true",
                        help="start the server, check that it works, then stop: for tests and CI, needs no display")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="openberry-desktop", description="OpenBerry in its own window.")
    add_arguments(parser)
    # A double-clicked app must start whatever it is given, e.g. "-psn_0_1234" from older macOS Finders.
    args, ignored = parser.parse_known_args(list(argv))
    args.ignored = ignored
    return args


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of the desktop app: `openberry-desktop` and the packaged executable."""
    if getattr(sys, "frozen", False):
        multiprocessing.freeze_support()  # a packaged Windows app must not re-launch itself for child processes
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in CLI_COMMANDS:
        from .cli import main as cli_main

        cli_main(args)
        return
    if code := run(parse_args(args)):
        raise SystemExit(code)


# --------------------------------------------------------------------------------------
# Logging: a packaged app has no console, so it writes a small rotating log file
# --------------------------------------------------------------------------------------

def _usable(stream: Any) -> bool:
    return stream is not None and callable(getattr(stream, "write", None))


def configure_logging(home: Path) -> Path | None:
    """Log to stderr, and to OPENBERRY_HOME/logs/desktop.log when packaged or without a console.

    Returns the log file, if there is one.
    """
    handlers: list[logging.Handler] = []
    log_file: Path | None = None
    if getattr(sys, "frozen", False) or not (_usable(sys.stdout) and _usable(sys.stderr)):
        try:
            (home / "logs").mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(home / "logs" / "desktop.log", maxBytes=LOG_FILE_BYTES,
                                               backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
        except OSError:
            pass  # a read-only home: run without a log file rather than not at all
        else:
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            handlers.append(file_handler)
            log_file = home / "logs" / "desktop.log"
    if _usable(sys.stderr):
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                        handlers=handlers or [logging.NullHandler()], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs full request URLs at INFO; webhook URLs are secrets
    return log_file


def _ensure_std_streams() -> None:
    """A packaged windowed app may have no stdout/stderr (None). Give libraries a sink that accepts writes."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 (lives as long as the app)


# --------------------------------------------------------------------------------------
# One app per user: desktop.json names the running server
# --------------------------------------------------------------------------------------

def write_state(home: Path, url: str) -> None:
    """Write OPENBERRY_HOME/desktop.json for this process's server (atomically: readers never see half a file)."""
    path = home / DESKTOP_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"url": url, "pid": os.getpid(), "version": __version__}), encoding="utf-8")
    os.replace(tmp, path)


def remove_state(home: Path, url: str) -> None:
    """Delete desktop.json if it still names this process's server (a newer app may have replaced it)."""
    path = home / DESKTOP_STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("url") == url and data.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


def is_local_url(url: str) -> bool:
    """True for http://127.0.0.1:<port> and the like: the only addresses opened from desktop.json."""
    try:
        parts = urlsplit(url)
        return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS and parts.port is not None
    except ValueError:
        return False


def answers_like_openberry(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """True if `url`/healthz answers as OpenBerry's does."""
    import httpx

    try:
        # trust_env=False: never send a local address through an HTTP(S)_PROXY.
        resp = httpx.get(f"{url}/healthz", timeout=timeout, trust_env=False)
        return resp.status_code == 200 and resp.json() == {"ok": True}
    except (httpx.HTTPError, ValueError):
        return False


def running_instance(home: Path) -> str | None:
    """The URL of an OpenBerry app already running for this OPENBERRY_HOME, or None.

    A desktop.json left behind by a crash names a server that no longer answers: ignored.
    """
    url = config.desktop_url(home)
    return url if url and is_local_url(url) and answers_like_openberry(url) else None


# --------------------------------------------------------------------------------------
# The dashboard server, in a background thread
# --------------------------------------------------------------------------------------

def bind_port(ports: Sequence[int], host: str = HOST) -> socket.socket:
    """A listening socket on the first free port of `ports`.

    The server is handed this socket, so no other program can take the port between the check and the start.
    """
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":  # never share a port with another program
                sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE"), 1)
            else:  # as uvicorn does: a port the last run left in TIME_WAIT is free
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen()
        except OSError:
            sock.close()
            continue
        return sock
    if len(ports) == 1:
        raise DesktopError(f"Port {ports[0]} is already in use. Close the program using it, or choose another port.")
    raise DesktopError(f"No free port between {ports[0]} and {ports[-1]}. Close some programs and try again.")


def socket_url(sock: socket.socket) -> str:
    host, port = sock.getsockname()[:2]
    return f"http://{host}:{port}"


def desktop_settings(url: str, home: Path) -> Settings:
    """Settings from the environment and .env files, with this app's address and a lasting session key."""
    settings = Settings.from_env()
    if not os.environ.get("OPENBERRY_BASE_URL"):  # the user's own setting wins
        # Links on the dashboard and in Claude's replies; the Host check accepts it too.
        os.environ["OPENBERRY_BASE_URL"] = settings.base_url = url
    if not os.environ.get("OPENBERRY_SECRET_KEY"):
        settings.secret_key = persistent_secret_key(home)
    return settings


def persistent_secret_key(home: Path) -> str:
    """The key that signs session cookies, kept in OPENBERRY_HOME so a login survives restarting the app.

    The window keeps its cookies (webview.start(private_mode=False)); with a new key on every start
    they would be worthless. If the file can't be written, the key lasts until the app closes.
    """
    path = home / SECRET_KEY_FILE
    with contextlib.suppress(OSError):
        if len(key := path.read_text(encoding="utf-8").strip()) >= 32:
            return key
    key = secrets.token_hex(32)
    try:
        home.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # readable by this user only
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError as exc:
        log.warning("could not save the session key (%s): you will have to log in again after a restart", exc)
    return key


@dataclass
class DashboardServer:
    """The dashboard (uvicorn) serving in a background thread of this process."""

    url: str
    server: uvicorn.Server
    thread: threading.Thread

    def running(self) -> bool:
        return self.thread.is_alive()

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)
        if self.thread.is_alive():
            log.warning("the server did not stop within %.0f seconds", timeout)


def _serve(server: uvicorn.Server, sock: socket.socket) -> None:
    try:
        server.run(sockets=[sock])
    except Exception:  # a thread's traceback would go to a console the packaged app doesn't have
        log.exception("the OpenBerry server stopped")


def start_server(settings: Settings, sock: socket.socket, timeout: float = STARTUP_TIMEOUT) -> DashboardServer:
    """Serve the dashboard on `sock` in a daemon thread; returns once it accepts requests."""
    import uvicorn

    from .web.app import create_app

    host, port = sock.getsockname()[:2]
    server = uvicorn.Server(uvicorn.Config(
        create_app(settings), host=host, port=port, log_level="info",
        log_config=None,  # log through our handlers: uvicorn's own config needs a console
        access_log=False, timeout_graceful_shutdown=5,
    ))
    thread = threading.Thread(target=_serve, args=(server, sock), name="openberry-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive():
            sock.close()
            raise DesktopError("The OpenBerry server stopped while starting. The log has the details.")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise DesktopError(f"The OpenBerry server did not start within {timeout:.0f} seconds.")
        time.sleep(0.05)
    log.info("OpenBerry %s is running at %s (data: %s)", __version__, socket_url(sock), settings.db_path)
    return DashboardServer(socket_url(sock), server, thread)


# --------------------------------------------------------------------------------------
# Window, or the browser
# --------------------------------------------------------------------------------------

def open_window(url: str, home: Path) -> bool:
    """Show `url` in a native window and return when the person closes it.

    False when no window could be shown: pywebview isn't installed, there is no GUI toolkit
    (e.g. Linux without GTK/Qt WebKit) or, on Windows, only Internet Explorer's engine.
    """
    try:
        import webview
    except ImportError:
        log.info("pywebview is not installed (pip install 'openberry[desktop]'): using the browser")
        return False
    logging.getLogger("pywebview").handlers.clear()  # its console handler; ours write to the log file too
    webview.settings["ALLOW_DOWNLOADS"] = True  # CSV export
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # target=_blank: LinkedIn, signal sources, docs
    fallback: list[str] = []

    def check_engine(window: Any) -> None:
        # Windows without the WebView2 runtime falls back to Internet Explorer, which can't run the dashboard.
        if webview.renderer == "mshtml":
            fallback.append("Microsoft Edge WebView2 Runtime is missing")
            window.events.shown.wait(15)
            window.destroy()

    try:
        window = webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                                       min_size=MIN_WINDOW_SIZE)
        # private_mode=False keeps cookies (the login) between runs, in OPENBERRY_HOME rather than
        # pywebview's folder shared by every pywebview app.
        webview.start(check_engine, (window,), private_mode=False, storage_path=str(home / "webview"))
    except Exception:
        log.warning("could not open a window: using the browser", exc_info=True)
        return False
    if fallback:
        log.warning("%s: using the browser", fallback[0])
        return False
    return True


def open_browser(url: str) -> None:
    if not webbrowser.open(url):
        log.warning("could not open a browser: open %s yourself", url)


@contextlib.contextmanager
def _stop_on_signals(stop: threading.Event) -> Iterator[None]:
    """Ctrl+C and SIGTERM set `stop` (only the main thread can handle signals)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    wanted = [signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])]
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in wanted}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)


def serve_until_stopped(server: DashboardServer, stop: threading.Event) -> None:
    """Keep the server running until Ctrl+C, SIGTERM or `stop` is set."""
    log.info("OpenBerry is running at %s. Press Ctrl+C to stop it.", server.url)
    with _stop_on_signals(stop):
        while not stop.wait(0.5):
            if not server.running():
                log.error("the OpenBerry server stopped unexpectedly")
                return


def show(url: str, home: Path, window: bool, server: DashboardServer | None = None,
         stop: threading.Event | None = None) -> None:
    """Show the dashboard in a window, else in the browser; with `server`, serve until the person is done."""
    if window and open_window(url, home):
        return
    open_browser(url)
    if server is not None:
        serve_until_stopped(server, stop or threading.Event())


def show_error(message: str, log_file: Path | None) -> None:
    """Tell the person why the app didn't start: a double-clicked app has no console to print to."""
    try:
        import webview
    except ImportError:
        return
    where = f"<p>Details are in <code>{html.escape(str(log_file))}</code>.</p>" if log_file else ""
    page = (f"<!doctype html><meta charset='utf-8'><body style='font:15px system-ui,sans-serif;margin:24px'>"
            f"<h2>OpenBerry could not start</h2><p>{html.escape(message)}</p>{where}</body>")
    with contextlib.suppress(Exception):
        webview.create_window(f"{WINDOW_TITLE}: could not start", html=page, width=560, height=260)
        webview.start()


# --------------------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------------------

def run(args: argparse.Namespace, stop: threading.Event | None = None) -> int:
    """Run the desktop app (`openberry desktop`). Returns the exit code.

    `stop` ends a browser-only run, as Ctrl+C does.
    """
    home = config.OPENBERRY_HOME
    log_file = configure_logging(home)
    _ensure_std_streams()
    if ignored := getattr(args, "ignored", None):
        log.info("ignoring unknown arguments: %s", " ".join(ignored))
    ports = DEFAULT_PORTS if args.port is None else [args.port]
    try:
        if args.smoke:
            return smoke(home, ports)
        _run_app(home, ports, window=not args.no_window, stop=stop)
    except DesktopError as exc:
        log.error("%s", exc)
        if not args.no_window and not args.smoke:
            show_error(str(exc), log_file)
        return 1
    except KeyboardInterrupt:  # Ctrl+C while a window was open; the server has been stopped
        return 130
    return 0


def _run_app(home: Path, ports: Sequence[int], window: bool, stop: threading.Event | None) -> None:
    if url := running_instance(home):
        log.info("OpenBerry is already running at %s: showing it", url)
        show(url, home, window)
        return
    sock = bind_port(ports)
    url = socket_url(sock)
    try:
        server = start_server(desktop_settings(url, home), sock)
    except BaseException:
        sock.close()
        raise
    write_state(home, url)
    try:
        show(url, home, window, server=server, stop=stop)
    finally:
        server.stop()
        remove_state(home, url)


def smoke_problems(url: str) -> list[str]:
    """What is broken in a running app, by checking what a packaged build can miss: the server,
    templates and the database, static files and the MCP server."""
    import httpx

    problems = []
    checks = (("/healthz", lambda r: r.json() == {"ok": True}), ("/", lambda r: "OpenBerry" in r.text),
              ("/static/app.css", lambda r: bool(r.content)))
    with httpx.Client(base_url=url, timeout=10, trust_env=False, follow_redirects=True) as client:
        for path, ok in checks:
            try:
                resp = client.get(path)
                if resp.status_code != 200 or not ok(resp):
                    problems.append(f"GET {path} answered {resp.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                problems.append(f"GET {path} failed: {exc}")
    try:
        from .mcp_server import build_server

        build_server()
    except Exception as exc:
        problems.append(f"the MCP server can't be built: {exc!r}")
    return problems


def smoke(home: Path, ports: Sequence[int]) -> int:
    """Start the server, check it, stop it: proves a (packaged) app works, with no display needed.

    It leaves desktop.json alone, so a running app is never disturbed.
    """
    sock = bind_port(ports)
    url = socket_url(sock)
    settings = desktop_settings(url, home)
    settings.scheduler_enabled = False  # a check must not start anyone's scheduled scans
    try:
        server = start_server(settings, sock)
    except BaseException:
        sock.close()
        raise
    try:
        problems = smoke_problems(url)
    finally:
        server.stop()
    if problems:
        for problem in problems:
            log.error("smoke test: %s", problem)
        print(f"OpenBerry {__version__} smoke test FAILED at {url}: " + "; ".join(problems), file=sys.stderr)
        return 1
    log.info("smoke test passed at %s", url)
    print(f"OpenBerry {__version__} smoke test passed at {url}")
    return 0


if __name__ == "__main__":
    main()
