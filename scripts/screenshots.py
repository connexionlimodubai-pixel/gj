"""Make the README's screenshots (docs/screenshots/*.png) from fictional sample data. For developers only.

    OPENBERRY_HOME=/tmp/openberry-shots uv run --extra dev python scripts/screenshots.py

It needs the dev extra (Playwright) and a Chromium for it (`uv run playwright install chromium`, or --chromium PATH).
It is not part of OpenBerry: it lives outside src/, so it is in neither the wheel, the desktop app nor the Docker image.

What it does, in this order:
1. It refuses to run unless OPENBERRY_HOME names a new or empty folder, so it can never touch real data.
2. It reads no .env file and none of your OPENBERRY_* settings: no mailbox, no API keys, no webhooks.
3. It seeds the test-only sample company (tests/sample_data.py) under a made-up name, paused, with email sending,
   AI agent sending and auto-approve off (as every new company has them).
4. It starts `openberry serve` on 127.0.0.1 and photographs the pages with Chromium. Pages that name the data folder
   show it as ~/.openberry, as on a normal install, not as this run's folder.

Nothing leaves the computer: in this script and in the server it starts, any connection to an address other than
127.0.0.1 or ::1 fails (Google, a mail server, a webhook...), and the browser can only load the local dashboard.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
# Fictional, and not the old demo's name: the screenshots must not look like that demo.
COMPANY = "Saffron Road Chauffeurs"
BRAND = "Saffron Road"
VIEWPORT = {"width": 1366, "height": 900}

# Installed in this process and in the server's: only loopback connections and name lookups are allowed.
NO_NETWORK = r'''
import socket as _socket

_LOCAL = {"127.0.0.1", "::1", "localhost", None}
_connect, _connect_ex, _getaddrinfo = _socket.socket.connect, _socket.socket.connect_ex, _socket.getaddrinfo


def _check(host):
    if host not in _LOCAL:
        raise OSError(f"scripts/screenshots.py allows no network: {host}")


def _host(address):
    return address[0] if isinstance(address, tuple) else None  # a Unix socket path is local


def _local_connect(self, address):
    _check(_host(address))
    return _connect(self, address)


def _local_connect_ex(self, address):
    _check(_host(address))
    return _connect_ex(self, address)


def _local_getaddrinfo(host, *args, **kwargs):
    _check(host.decode() if isinstance(host, bytes) else host)
    return _getaddrinfo(host, *args, **kwargs)


_socket.socket.connect, _socket.socket.connect_ex = _local_connect, _local_connect_ex
_socket.getaddrinfo = _local_getaddrinfo
'''

# The server: `openberry serve`, after the same guard.
SERVE = NO_NETWORK + r'''
import sys
from openberry.cli import main
main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1]])
'''

# The only variables the server gets from this environment: none of them reaches the network or changes the pages.
KEEP_ENV = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
            "PYTHONPATH", "VIRTUAL_ENV"}
# Dropped from this process's own environment before it seeds: OpenBerry's settings, keys and proxies.
DROP_PREFIXES = ("OPENBERRY_", "REDDIT_", "SMTP", "IMAP")
DROP_NAMES = {"GITHUB_TOKEN", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
              "all_proxy"}


class Refused(Exception):
    pass


def data_folder(environ: dict[str, str]) -> Path:
    """OPENBERRY_HOME, created if it doesn't exist. Refused when it is unset or holds anything."""
    raw = environ.get("OPENBERRY_HOME", "").strip()
    if not raw:
        raise Refused("set OPENBERRY_HOME to a new, empty folder (the script fills it with sample data)")
    home = Path(raw).expanduser().resolve()
    if home.exists() and (not home.is_dir() or any(home.iterdir())):
        raise Refused(f"OPENBERRY_HOME ({home}) is not empty: use a new, empty folder, so no real data is touched")
    home.mkdir(parents=True, exist_ok=True)
    return home


def clean_env(environ: dict[str, str], home: Path, base_url: str) -> dict[str, str]:
    """The environment for this process's seeding and for the server: the data folder and nothing else."""
    env = {key: value for key, value in environ.items() if key in KEEP_ENV}
    env.update({
        "OPENBERRY_HOME": str(home),
        "OPENBERRY_DB": str(home / "openberry.db"),
        "OPENBERRY_ENV_FILE": str(home / ".env"),  # empty: the only .env file read (not ./.env or ~/.openberry/.env)
        "OPENBERRY_BASE_URL": base_url,
        "TZ": "Asia/Dubai",  # the sample company's clock, for the email sending card
        "PYTHONUNBUFFERED": "1",
    })
    return env


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def seed(env: dict[str, str]) -> dict[str, int]:
    """The sample company, paused, through tests/sample_data.py. Returns the ids the pages need."""
    for key in list(os.environ):
        if key.startswith(DROP_PREFIXES) or key in DROP_NAMES:
            del os.environ[key]
    os.environ.update({key: value for key, value in env.items() if key.startswith("OPENBERRY_") or key == "TZ"})
    sys.path.insert(0, str(ROOT / "tests"))
    from sample_data import seed_sample_company  # tests/sample_data.py: test-only, never shipped

    from openberry import repo

    company_id = seed_sample_company(name=COMPANY, status="paused", brand=BRAND)
    company = repo.get_company(company_id)
    assert company is not None and company.status == "paused"
    outreach = company.outreach
    assert not (outreach.email_sending or outreach.agent_sending or outreach.auto_approve), "sending must be off"
    leads, _ = repo.list_leads(company_id, kind="person", limit=1)
    return {"company": company_id, "lead": leads[0].id}


@contextlib.contextmanager
def server(env: dict[str, str], port: int, log: Path) -> Iterator[str]:
    base = f"http://127.0.0.1:{port}"
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen([sys.executable, "-c", SERVE, str(port)], env=env, stdout=out,
                                stderr=subprocess.STDOUT, cwd=env["OPENBERRY_HOME"])
        try:
            deadline = time.monotonic() + 30
            while True:
                if proc.poll() is not None:
                    raise Refused(f"openberry serve exited with code {proc.returncode}:\n{log.read_text()[-2000:]}")
                with contextlib.suppress(OSError, ValueError):
                    with urllib.request.urlopen(base + "/healthz", timeout=2) as response:
                        if json.load(response).get("app") == "openberry":
                            break
                if time.monotonic() > deadline:
                    raise Refused("openberry serve did not answer /healthz within 30 seconds")
                time.sleep(0.2)
            yield base
        finally:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()


# (file name, path, whole page?) The README shows the first four; the others are for the docs and reviews.
def pages(ids: dict[str, int]) -> list[tuple[str, str, bool]]:
    c, lead = ids["company"], ids["lead"]
    return [
        ("dashboard.png", f"/c/{c}", True),
        ("registration-board.png", "/companies", False),
        ("register.png", "/register", False),
        ("lead.png", f"/c/{c}/leads/{lead}", True),
        ("outreach.png", f"/c/{c}/outreach", True),
        ("mailbox.png", "/mailbox", True),
    ]


def launch(pw: object, chromium: str | None) -> object:
    candidates: list[str | None] = [chromium] if chromium else [None]
    if not chromium and (root := os.environ.get("PLAYWRIGHT_BROWSERS_PATH")):
        candidates += sorted(str(p) for p in Path(root).glob("chromium-*/chrome-*/chrome"))
    error: Exception | None = None
    for executable in candidates:
        try:
            return pw.chromium.launch(executable_path=executable)  # type: ignore[attr-defined]
        except Exception as exc:  # playwright's Error: try the next one
            error = exc
    raise Refused(f"no Chromium for Playwright: run `uv run playwright install chromium` or pass --chromium ({error})")


# Shows the data folder as a normal install's (~/.openberry) instead of this run's temporary folder.
SHOW_HOME = """([from, to]) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeValue.includes(from)) node.nodeValue = node.nodeValue.split(from).join(to);
  }
}"""


def photograph(base: str, home: Path, shots: list[tuple[str, str, bool]], out: Path, chromium: str | None
               ) -> list[Path]:
    from playwright.sync_api import sync_playwright

    written = []
    with sync_playwright() as pw:
        browser = launch(pw, chromium)
        try:
            context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1, color_scheme="light",
                                          locale="en-GB", timezone_id="Asia/Dubai")
            # The browser loads the local dashboard only: no fonts, maps or anything else from the internet.
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base)
                          else route.abort())
            page = context.new_page()
            for name, path, full_page in shots:
                page.set_viewport_size(VIEWPORT)
                page.goto(base + path, wait_until="networkidle")
                if "demo" in page.inner_text("body").lower():
                    raise Refused(f"{path} says 'demo': the screenshots must not show the old demo")
                page.evaluate(SHOW_HOME, [str(home), "~/.openberry"])
                if full_page:
                    # A window as tall as the page, rather than Playwright's full_page: the menu then runs the whole
                    # height and sticky bars sit where they belong instead of over the content.
                    for _ in range(3):
                        height = page.evaluate("document.documentElement.scrollHeight")
                        if height == page.viewport_size["height"]:
                            break
                        page.set_viewport_size({"width": VIEWPORT["width"], "height": max(height, VIEWPORT["height"])})
                        page.wait_for_timeout(200)
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(out / name))
                written.append(out / name)
        finally:
            browser.close()
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=OUT, help=f"where the PNG files go (default: {OUT})")
    parser.add_argument("--chromium", help="a Chromium executable for Playwright (default: Playwright's own)")
    args = parser.parse_args(argv)
    try:
        home = data_folder(dict(os.environ))
        port = free_port()
        env = clean_env(dict(os.environ), home, f"http://127.0.0.1:{port}")
        (home / ".env").write_text("", encoding="utf-8")
        exec(NO_NETWORK, {})  # noqa: S102 - the same guard as the server's, for the seeding
        ids = seed(env)
        args.out.mkdir(parents=True, exist_ok=True)
        with server(env, port, home / "serve.log") as base:
            written = photograph(base, home, pages(ids), args.out, args.chromium)
    except Refused as exc:
        print(f"scripts/screenshots.py: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
