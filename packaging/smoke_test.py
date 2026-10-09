"""Check a built OpenBerry app end to end: `python packaging/smoke_test.py <openberry-cli> [<OpenBerry>]`.

With a fresh, temporary OPENBERRY_HOME (your own data is never touched) it runs, in order:

  1. `openberry-cli --version`
  2. `openberry-cli demo`                      the database and the demo data
  3. `openberry-cli serve --port <free>`       /healthz, the demo dashboard /c/1, /help and a static file
  4. `openberry-cli mcp`                       what Claude Desktop starts: an MCP handshake over stdio,
                                               tools/list (21 tools) and a tool call that sees the demo company
  5. `OpenBerry --smoke` (if given)            the app's executable starts its server, checks it and stops;
                                               a windowed Windows app has no console, so its log file counts too

It stops at the first failure and exits 1 with a message saying what broke. It needs only the
Python standard library, so any Python 3.10+ can run it against a build.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

PROTOCOL_VERSION = "2025-06-18"
EXPECTED_TOOLS = 21
COMMAND_TIMEOUT = 120.0  # seconds; a first start of a packaged app can be slow (antivirus scans on Windows)
SERVER_TIMEOUT = 90.0
MCP_TIMEOUT = 60.0
SMOKE_PASSED = "smoke test passed"
LOG_TAIL = 3000


class SmokeFailure(Exception):
    """A check failed; the message says what broke."""


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def clean_env(home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for the app: `home` as OPENBERRY_HOME, none of the caller's OpenBerry settings.

    Scheduled scans are off (a smoke test must not reach the internet), and Python output is unbuffered.
    """
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.startswith("OPENBERRY_")}
    env.update({
        "OPENBERRY_HOME": str(home),
        "OPENBERRY_ENV_FILE": str(home / "smoke.env"),  # read no .env file but this (missing) one
        "OPENBERRY_SCHEDULER": "false",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def tail(text: str, limit: int = LOG_TAIL) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def run(command: Sequence[str], env: Mapping[str, str], cwd: Path, timeout: float = COMMAND_TIMEOUT
        ) -> subprocess.CompletedProcess[str]:
    """Run a command to its end; SmokeFailure if it doesn't finish in time."""
    try:
        return subprocess.run(list(command), env=dict(env), cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"`{' '.join(command)}` did not finish within {timeout:.0f} seconds") from exc


def expect_success(result: subprocess.CompletedProcess[str], what: str) -> str:
    """The command's stdout; SmokeFailure with its output if it failed."""
    if result.returncode != 0:
        raise SmokeFailure(f"{what} exited with code {result.returncode}\n"
                           f"stdout: {tail(result.stdout)}\nstderr: {tail(result.stderr)}")
    return result.stdout


def http_get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """GET `url` directly (never through a proxy): (status, body). Raises OSError if it can't connect."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def is_openberry_health(data: Any) -> bool:
    """OpenBerry's /healthz answer, {"ok": true, "app": "openberry"}: not another program on the port."""
    return isinstance(data, dict) and data.get("ok") is True and data.get("app") == "openberry"


def wait_until_healthy(url: str, proc: subprocess.Popen[Any], timeout: float = SERVER_TIMEOUT,
                       interval: float = 0.25) -> None:
    """Poll `url`/healthz until OpenBerry answers; SmokeFailure if the server exits or times out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SmokeFailure(f"the server exited with code {proc.returncode} before it answered")
        try:
            status, body = http_get(f"{url}/healthz", timeout=2)
            if status == 200 and is_openberry_health(json.loads(body)):
                return
        except (OSError, ValueError):
            pass
        time.sleep(interval)
    raise SmokeFailure(f"{url}/healthz did not answer within {timeout:.0f} seconds")


def stop(proc: subprocess.Popen[Any], timeout: float = 15.0) -> None:
    """Stop a process gently (SIGTERM; TerminateProcess on Windows), then for good."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class LineReader:
    """Reads a process's output lines in a thread, so a read can time out."""

    def __init__(self, stream: IO[str]) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self.thread.start()

    def _pump(self, stream: IO[str]) -> None:
        for line in stream:
            self.lines.put(line)
        self.lines.put(None)

    def next_line(self, timeout: float) -> str | None:
        """The next line, or None at the end of the output. SmokeFailure if none comes in time."""
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            raise SmokeFailure(f"no answer within {timeout:.0f} seconds") from None


def jsonrpc_response(reader: LineReader, request_id: int, timeout: float = MCP_TIMEOUT) -> dict[str, Any]:
    """Read JSON-RPC messages up to the response to `request_id`.

    Every line on the MCP server's stdout must be JSON-RPC: anything else (a print, a log line)
    breaks Claude Desktop's connection.
    """
    deadline = time.monotonic() + timeout
    while True:
        line = reader.next_line(max(0.1, deadline - time.monotonic()))
        if line is None:
            raise SmokeFailure("the MCP server closed its output before it answered")
        not_jsonrpc = f"the MCP server wrote something that is not JSON-RPC to stdout: {line[:200]!r}"
        try:
            message = json.loads(line)
        except ValueError:
            raise SmokeFailure(not_jsonrpc) from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise SmokeFailure(not_jsonrpc)
        if message.get("id") == request_id:
            if "error" in message:
                raise SmokeFailure(f"the MCP server answered request {request_id} with an error: {message['error']}")
            return message


def read_log(home: Path) -> str:
    """The desktop app's log (OPENBERRY_HOME/logs/desktop.log), or ""."""
    try:
        return (home / "logs" / "desktop.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------------------

def check_version(cli: Path, env: Mapping[str, str], cwd: Path) -> str:
    out = expect_success(run([str(cli), "--version"], env, cwd), "--version").strip()
    if not out.startswith("openberry "):
        raise SmokeFailure(f"--version printed {out!r}, expected 'openberry <version>'")
    return out


def check_demo(cli: Path, env: Mapping[str, str], cwd: Path) -> str:
    out = expect_success(run([str(cli), "demo"], env, cwd), "demo")
    if "Demo company created with id 1" not in out:
        raise SmokeFailure(f"demo printed {tail(out)!r}, expected 'Demo company created with id 1'")
    return "demo company 1 created"


def server_pages(cli: Path) -> list[tuple[str, str]]:
    """(path, text the page must contain) for each page `serve` must answer."""
    return [
        ("/c/1", "Demo"),  # the demo company's dashboard: templates, database and the demo data
        ("/help", cli.name),  # Claude Desktop's config starts this very command line (bundled_cli)
        ("/static/app.css", "--"),  # static files are bundled
    ]


def check_serve(cli: Path, env: Mapping[str, str], cwd: Path) -> str:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    pages = server_pages(cli)
    proc = subprocess.Popen([str(cli), "serve", "--port", str(port)], env={**env, "OPENBERRY_BASE_URL": url},
                            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    output: list[str] = []
    pump = threading.Thread(target=lambda: output.extend(proc.stdout or ()), daemon=True)
    pump.start()
    try:
        wait_until_healthy(url, proc)
        for path, expected in pages:
            status, body = http_get(url + path)
            if status != 200:
                raise SmokeFailure(f"GET {path} answered {status}")
            if expected not in body:
                raise SmokeFailure(f"GET {path} answered without {expected!r}")
    except (SmokeFailure, OSError) as exc:
        stop(proc)
        pump.join(5)
        raise SmokeFailure(f"serve: {exc}\nserver output: {tail(''.join(output))}") from exc
    stop(proc)
    return f"served {', '.join(path for path, _ in pages)} at {url}"


def check_mcp(cli: Path, env: Mapping[str, str], cwd: Path, expected_tools: int = EXPECTED_TOOLS) -> str:
    proc = subprocess.Popen([str(cli), "mcp"], env=dict(env), cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace")
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    reader = LineReader(proc.stdout)
    stderr: list[str] = []
    threading.Thread(target=lambda: stderr.extend(proc.stderr or ()), daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                         "clientInfo": {"name": "openberry-smoke-test", "version": "1"}}})
        init = jsonrpc_response(reader, 1)["result"]
        if init.get("serverInfo", {}).get("name") != "openberry":
            raise SmokeFailure(f"initialize answered serverInfo {init.get('serverInfo')!r}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [tool["name"] for tool in jsonrpc_response(reader, 2)["result"]["tools"]]
        if len(tools) != expected_tools:
            raise SmokeFailure(f"tools/list returned {len(tools)} tools, expected {expected_tools}: {tools}")
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_companies", "arguments": {}}})
        called = jsonrpc_response(reader, 3)["result"]
        companies = (called.get("structuredContent") or {}).get("companies") or []
        if called.get("isError") or not companies:
            raise SmokeFailure(f"list_companies did not see the demo company: {json.dumps(called)[:500]}")
        proc.stdin.close()  # Claude Desktop closes stdin to stop the server
        proc.wait(timeout=30)
    except (SmokeFailure, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        detail = exc if isinstance(exc, SmokeFailure) else f"{type(exc).__name__}: {exc}"
        stop(proc)
        raise SmokeFailure(f"mcp: {detail}\nstderr: {tail(''.join(stderr))}") from exc
    finally:
        stop(proc)
    return (f"protocol {init.get('protocolVersion')}, {len(tools)} tools, "
            f"list_companies sees {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}")


def check_app(app: Path, env: Mapping[str, str], cwd: Path, home: Path) -> str:
    result = run([str(app), "--smoke"], env, cwd)
    log = read_log(home)
    passed = SMOKE_PASSED in result.stdout or SMOKE_PASSED in log
    if result.returncode != 0 or not passed:
        raise SmokeFailure(f"`{app.name} --smoke` exited with code {result.returncode}"
                           + ("" if passed else f" without saying {SMOKE_PASSED!r}")
                           + f"\nstdout: {tail(result.stdout)}\nstderr: {tail(result.stderr)}\n"
                           f"log ({home / 'logs' / 'desktop.log'}): {tail(log) or '(none)'}")
    return "--smoke passed"


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def checks_for(cli: Path, app: Path | None, home: Path, cwd: Path,
               expected_tools: int = EXPECTED_TOOLS) -> list[tuple[str, Callable[[], str]]]:
    env = clean_env(home)
    checks: list[tuple[str, Callable[[], str]]] = [
        ("version", lambda: check_version(cli, env, cwd)),
        ("demo", lambda: check_demo(cli, env, cwd)),
        ("serve", lambda: check_serve(cli, env, cwd)),
        ("mcp", lambda: check_mcp(cli, env, cwd, expected_tools)),
    ]
    if app is not None:
        checks.append(("app --smoke", lambda: check_app(app, env, cwd, home)))
    return checks


def executable(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"{value} is not a file")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise argparse.ArgumentTypeError(f"{value} is not executable")
    return path


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python packaging/smoke_test.py",
                                     description="Check a built OpenBerry app end to end.")
    parser.add_argument("cli", type=executable, help="the console executable, e.g. dist/OpenBerry/openberry-cli")
    parser.add_argument("app", type=executable, nargs="?", help="the app's executable, e.g. dist/OpenBerry/OpenBerry")
    parser.add_argument("--expect-tools", type=int, default=EXPECTED_TOOLS, help="MCP tools expected (default: 21)")
    return parser.parse_args(argv)


def safe_output() -> None:
    """Never crash on printing: a Windows console (cp1252) can't show every character a page or log holds."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    safe_output()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="openberry-smoke-", ignore_cleanup_errors=True) as tmp:
        home, cwd = Path(tmp) / "home", Path(tmp)
        for name, check in checks_for(args.cli, args.app, home, cwd, args.expect_tools):
            try:
                detail = check()
            except SmokeFailure as exc:
                print(f"FAIL {name}: {exc}", file=sys.stderr, flush=True)
                print(f"Smoke test FAILED for {args.cli}", file=sys.stderr)
                return 1
            print(f"ok   {name}: {detail}", flush=True)
    print(f"Smoke test passed in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
