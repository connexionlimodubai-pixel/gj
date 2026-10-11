"""Security hardening (WP7): start-up checks for a server other computers can reach, throttled API tokens, private
files, trusted proxies, response headers, logs without personal data and the OpenAPI schema behind auth.

Local mode (127.0.0.1, no password: the desktop app and a plain `openberry serve`) must work exactly as before, with
no login: every check here leaves it alone.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from openberry import cli, config, db, repo, services
from openberry.config import Settings, start_check
from openberry.models import CompanyIn, LeadIn
from openberry.web.app import UnsafeStart, create_app

STRONG = "correct-horse-battery-staple"  # 28 characters
TOKEN = "t" * 40
posix_only = pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX file modes")


@pytest.fixture
def web_settings(settings: Settings) -> Settings:
    settings.scheduler_enabled = False
    settings.http_mcp_enabled = False
    settings.allowed_hosts = ["testserver"]
    return settings


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ======================================================================================
# 7.2 Refuse unsafe server starts
# ======================================================================================

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"])
def test_loopback_needs_nothing(settings: Settings, host: str) -> None:
    check = start_check(settings, host, environ={})
    assert check.refusals == [] and check.warnings == []


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "leads.example.com"])
def test_a_network_address_without_a_password_is_refused(settings: Settings, host: str) -> None:
    [refusal] = start_check(settings, host, environ={}).refusals
    assert "no password" in refusal and "OPENBERRY_PASSWORD" in refusal and "OPENBERRY_ALLOW_NO_PASSWORD" in refusal
    assert host in refusal


def test_allow_no_password_starts_with_a_warning_about_the_open_api(settings: Settings) -> None:
    settings.allow_no_password = True
    check = start_check(settings, "0.0.0.0", environ={})
    assert check.refusals == []
    assert any("anyone who reaches OpenBerry can use its JSON API" in w for w in check.warnings)


def test_a_short_password_is_refused_with_the_fix(settings: Settings) -> None:
    settings.password = "a"
    [refusal] = start_check(settings, "0.0.0.0", environ={}).refusals
    assert "too short" in refusal and "12 characters" in refusal and config.SECRET_COMMAND in refusal
    settings.password = "x" * 12
    assert start_check(settings, "0.0.0.0", environ={}).refusals == []
    settings.password = "a"  # only this computer can open it: a short password is the user's business
    assert start_check(settings, "127.0.0.1", environ={}).refusals == []


def test_weak_secrets_and_plain_http_are_warned_about(settings: Settings) -> None:
    settings.password = STRONG
    settings.base_url = "http://leads.example.com"
    warnings = start_check(settings, "0.0.0.0", environ={}).warnings
    text = "\n".join(warnings)
    assert "OPENBERRY_SECRET_KEY is not set" in text
    assert "OPENBERRY_API_TOKEN is not set, so the JSON API and /mcp are off" in text
    assert "http://leads.example.com" in text and "https://" in text
    assert config.SECRET_COMMAND in text

    settings.api_token = "short"
    warnings = start_check(settings, "0.0.0.0", environ={"OPENBERRY_SECRET_KEY": "abc"}).warnings
    assert any("OPENBERRY_SECRET_KEY is shorter than 32" in w for w in warnings)
    assert any("OPENBERRY_API_TOKEN is shorter than 32" in w for w in warnings)

    settings.api_token = TOKEN
    settings.base_url = "https://leads.example.com"
    assert start_check(settings, "0.0.0.0", environ={"OPENBERRY_SECRET_KEY": "k" * 32}).warnings == []


def test_the_default_base_url_on_a_network_address_is_not_called_unencrypted(settings: Settings) -> None:
    """Docker: listening on 0.0.0.0, opened at http://127.0.0.1:8000 through compose's local-only port."""
    settings.password, settings.api_token = STRONG, TOKEN
    assert settings.base_url == config.DEFAULT_BASE_URL
    assert start_check(settings, "0.0.0.0", environ={"OPENBERRY_SECRET_KEY": "k" * 40}).warnings == []


def test_create_app_refuses_and_warns_only_when_given_a_host(settings: Settings,
                                                             caplog: pytest.LogCaptureFixture) -> None:
    create_app(settings)  # the desktop app and the tests: no host, no check
    with pytest.raises(UnsafeStart, match="no password"):
        create_app(settings, host="0.0.0.0")
    settings.password = STRONG
    with caplog.at_level(logging.WARNING, logger="openberry.web.app"):
        create_app(settings, host="0.0.0.0")
    assert "OPENBERRY_API_TOKEN is not set" in caplog.text


@pytest.fixture
def no_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """uvicorn.run records its options instead of serving."""
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(options))
    return calls


def test_serve_on_all_addresses_with_a_one_letter_password_exits_with_the_reason(
        settings: Settings, no_uvicorn: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    settings.password = "a"
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--host", "0.0.0.0"])
    assert exc.value.code not in (0, None) and "OPENBERRY_PASSWORD is too short" in str(exc.value.code)
    assert no_uvicorn == []


def test_serve_on_loopback_is_unchanged(settings: Settings, no_uvicorn: list[dict[str, Any]]) -> None:
    cli.main(["serve"])
    [options] = no_uvicorn
    assert options["host"] == "127.0.0.1" and options["port"] == 8000 and options["proxy_headers"] is True


def test_the_server_command_refuses_for_real(tmp_path: Path) -> None:
    """The whole command, as `OPENBERRY_PASSWORD=a openberry serve --host 0.0.0.0` runs it."""
    env = {**os.environ, "OPENBERRY_HOME": str(tmp_path), "OPENBERRY_ENV_FILE": str(tmp_path / "none.env"),
           "OPENBERRY_PASSWORD": "a", "PYTHONPATH": os.pathsep.join(sys.path)}
    done = subprocess.run([sys.executable, "-m", "openberry", "serve", "--host", "0.0.0.0", "--port", "0"],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert done.returncode == 1 and "OPENBERRY_PASSWORD is too short" in done.stderr
    assert not (tmp_path / "openberry.db").exists()


@pytest.mark.parametrize("password, allow_no_password", [("", False), ("", True), (STRONG, False)])
def test_mcp_over_http_on_a_network_address_needs_an_api_token(
        settings: Settings, no_uvicorn: list[dict[str, Any]], password: str, allow_no_password: bool) -> None:
    """Without a token only the Host check guards it, and any computer can send `Host: localhost`. Its tools read,
    change and delete everything, so OPENBERRY_ALLOW_NO_PASSWORD doesn't open it."""
    settings.password, settings.allow_no_password = password, allow_no_password
    with pytest.raises(SystemExit) as exc:
        cli.main(["mcp", "--http", "--host", "0.0.0.0"])
    message = str(exc.value.code)
    assert message.startswith("openberry mcp: ") and "0.0.0.0" in message
    assert "OPENBERRY_API_TOKEN" in message and "32 characters" in message and "--host 127.0.0.1" in message
    assert "OPENBERRY_ALLOW_NO_PASSWORD" in message and config.SECRET_COMMAND in message
    assert no_uvicorn == []


def test_mcp_over_http_with_a_token_starts_and_warns_about_a_short_one(
        settings: Settings, no_uvicorn: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> None:
    settings.api_token = "short"
    with caplog.at_level(logging.WARNING):
        cli.main(["mcp", "--http", "--host", "0.0.0.0"])
        assert no_uvicorn[-1]["host"] == "0.0.0.0"
        assert "OPENBERRY_API_TOKEN is shorter than 32 characters, which makes it easier to guess" in caplog.text
        caplog.clear()
        settings.api_token = TOKEN
        cli.main(["mcp", "--http", "--host", "0.0.0.0"])
    assert len(no_uvicorn) == 2 and "OPENBERRY_API_TOKEN" not in caplog.text


def test_mcp_over_http_on_loopback_needs_no_token(settings: Settings, no_uvicorn: list[dict[str, Any]]) -> None:
    cli.main(["mcp", "--http"])
    [options] = no_uvicorn
    assert options["host"] == "127.0.0.1" and options["port"] == 8001


# ======================================================================================
# 7.3 Wrong bearer tokens are throttled (/api and /mcp)
# ======================================================================================

@pytest.fixture
def token_client(web_settings: Settings):
    web_settings.password = STRONG
    web_settings.api_token = TOKEN
    web_settings.http_mcp_enabled = True
    with TestClient(create_app(web_settings)) as client:
        yield client


def wrong(n: int) -> dict[str, str]:
    return {"Authorization": f"Bearer guess-{n}"}


MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def test_the_21st_wrong_token_on_the_api_gets_429(token_client: TestClient) -> None:
    for n in range(20):
        assert token_client.get("/api/companies", headers=wrong(n)).status_code == 401
    refused = token_client.get("/api/companies", headers=wrong(21))
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) > 0
    assert "Too many wrong API tokens" in refused.json()["detail"]
    # Even the right token waits: otherwise guessing could go on.
    assert token_client.get("/api/companies", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 429


def test_the_21st_wrong_token_on_mcp_gets_429(token_client: TestClient) -> None:
    for n in range(20):
        assert token_client.post("/mcp", json=INIT, headers={**MCP_HEADERS, **wrong(n)}).status_code == 401
    refused = token_client.post("/mcp", json=INIT, headers={**MCP_HEADERS, **wrong(21)})
    assert refused.status_code == 429 and refused.headers["retry-after"]
    assert refused.json()["error"] == "too_many_attempts"


def test_api_and_mcp_share_one_budget_and_it_expires(token_client: TestClient) -> None:
    for n in range(10):
        token_client.get("/api/companies", headers=wrong(n))
        token_client.post("/mcp", json=INIT, headers={**MCP_HEADERS, **wrong(n)})
    assert token_client.get("/api/companies", headers=wrong(99)).status_code == 429
    limits = token_client.app.state.limits.token_failures
    later = time.monotonic() + 601
    assert limits.retry_after("testclient", now=later) == 0


def test_the_right_token_and_requests_without_one_never_count(token_client: TestClient) -> None:
    good = {"Authorization": f"Bearer {TOKEN}"}
    for _ in range(25):
        assert token_client.get("/api/companies", headers=good).status_code == 200
        assert token_client.get("/api/companies").status_code == 401  # no token: not a guess
    assert token_client.app.state.limits.token_failures.count() == 0
    assert token_client.post("/mcp", json=INIT, headers={**MCP_HEADERS, **good}).status_code == 200


def test_local_mode_has_no_token_to_throttle(web_settings: Settings) -> None:
    with TestClient(create_app(web_settings)) as client:
        for n in range(25):
            assert client.get("/api/companies", headers=wrong(n)).status_code == 200
        assert client.app.state.limits.token_failures.count() == 0


# ======================================================================================
# 7.4 Private files
# ======================================================================================

@posix_only
def test_a_new_data_folder_is_private_even_with_umask_022(tmp_path: Path, settings: Settings) -> None:
    from openberry import backups

    home = tmp_path / "new" / "home"
    settings.db_path = home / "openberry.db"
    os.umask(0o022)
    db.init_db(settings.db_path)
    with db.connect() as conn:
        conn.execute("INSERT INTO companies (name, created_at, updated_at) VALUES ('A', 'x', 'x')")
    snapshot = backups.manual_backup(None)
    db.reset_init_cache()
    db.init_db(settings.db_path)  # the -wal and -shm now exist too
    assert mode(tmp_path / "new") == 0o700 and mode(home) == 0o700 and mode(home / "backups") == 0o700
    for name in ("openberry.db", "openberry.db-wal", "openberry.db-shm"):
        if (home / name).exists():
            assert mode(home / name) == 0o600, name
    assert mode(snapshot) == 0o600


@posix_only
def test_the_commands_set_a_private_umask(tmp_path: Path, settings: Settings) -> None:
    settings.db_path = tmp_path / "data" / "openberry.db"
    os.umask(0o022)
    cli.main(["init-db"])
    assert os.umask(0o022) == config.PRIVATE_UMASK
    cli.main(["backup"])
    assert mode(tmp_path / "data") == 0o700 and mode(settings.db_path) == 0o600
    [copy] = (tmp_path / "data" / "backups").iterdir()
    assert mode(copy.parent) == 0o700 and mode(copy) == 0o600


@posix_only
def test_the_desktop_app_sets_a_private_umask_and_private_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openberry import desktop

    os.umask(0o022)
    monkeypatch.setattr(desktop, "run", lambda args, stop=None: 0)
    desktop.main(["--no-window"])
    assert os.umask(0o022) == config.PRIVATE_UMASK
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # a packaged app logs to a file
    try:
        desktop.configure_logging(tmp_path / "home")
    finally:
        logging.basicConfig(force=True, handlers=[logging.NullHandler()])
    assert mode(tmp_path / "home") == 0o700 and mode(tmp_path / "home" / "logs") == 0o700


@posix_only
def test_an_older_data_folder_and_its_env_file_are_made_private_at_start(
        tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.1.x left ~/.openberry at 0755 and a .env written by hand (tokens, passwords) at 0644. A database folder of
    the user's own choosing (OPENBERRY_DB) keeps its permissions."""
    from openberry import desktop

    home, shared = tmp_path / "home", tmp_path / "shared"
    monkeypatch.setattr(config, "OPENBERRY_HOME", home)
    monkeypatch.delenv("OPENBERRY_ENV_FILE")
    monkeypatch.setattr(desktop, "run", lambda args, stop=None: 0)
    settings.db_path = shared / "openberry.db"
    for start in (lambda: cli.main(["init-db"]), lambda: desktop.main(["--no-window"])):
        for folder in (home, shared):
            folder.mkdir(exist_ok=True)
            folder.chmod(0o755)
        (home / ".env").write_text("GITHUB_TOKEN=x\n", encoding="utf-8")
        (home / ".env").chmod(0o644)
        start()
        assert mode(home) == 0o700 and mode(home / ".env") == 0o600
        assert mode(shared) == 0o755


@posix_only
def test_another_accounts_data_folder_is_left_alone_and_a_failed_chmod_never_stops_the_start(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    (home / ".env").write_text("", encoding="utf-8")
    (home / ".env").chmod(0o644)
    monkeypatch.setattr(config, "OPENBERRY_HOME", home)
    monkeypatch.delenv("OPENBERRY_ENV_FILE")
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    config.private_files()
    assert mode(home) == 0o755 and mode(home / ".env") == 0o644

    def refuse(path: Path, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(os, "getuid", lambda: uid)
    monkeypatch.setattr(Path, "chmod", refuse)
    config.private_files()  # no error: OpenBerry starts anyway
    assert mode(home) == 0o755


@posix_only
def test_an_existing_folder_keeps_its_permissions(tmp_path: Path, settings: Settings) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    settings.db_path = shared / "openberry.db"
    db.init_db(settings.db_path)
    assert mode(shared) == 0o755 and mode(settings.db_path) == 0o600


# ======================================================================================
# 7.5 Trusted proxies
# ======================================================================================

def test_serve_passes_the_trusted_proxies_to_uvicorn(settings: Settings, no_uvicorn: list[dict[str, Any]]) -> None:
    cli.main(["serve"])
    assert no_uvicorn[-1]["forwarded_allow_ips"] is None  # uvicorn's default: 127.0.0.1
    settings.trusted_proxies = ["172.18.0.0/16", "10.0.0.2"]
    cli.main(["serve"])
    assert no_uvicorn[-1]["forwarded_allow_ips"] == ["172.18.0.0/16", "10.0.0.2"]


def test_trusted_proxies_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBERRY_TRUSTED_PROXIES", " 172.18.0.0/16 , 10.0.0.2 ")
    monkeypatch.setenv("OPENBERRY_ALLOW_NO_PASSWORD", "true")
    s = Settings.from_env()
    assert s.trusted_proxies == ["172.18.0.0/16", "10.0.0.2"] and s.allow_no_password is True


async def test_behind_a_trusted_proxy_each_visitor_has_their_own_login_limit(web_settings: Settings) -> None:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    web_settings.password = STRONG
    proxy = "172.18.0.5"
    app = ProxyHeadersMiddleware(create_app(web_settings), trusted_hosts=["172.18.0.0/16"])
    transport = httpx.ASGITransport(app=app, client=(proxy, 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        async def attempt(visitor: str) -> int:
            page = await client.get("/login", headers={"X-Forwarded-For": visitor})
            csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
            resp = await client.post("/login", data={"password": "wrong", "csrf_token": csrf, "next": "/"},
                                     headers={"X-Forwarded-For": visitor})
            return resp.status_code

        assert [await attempt("203.0.113.7") for _ in range(6)] == [401] * 5 + [429]
        assert await attempt("198.51.100.9") == 401  # another visitor behind the same proxy isn't locked out


# ======================================================================================
# 7.6 Response headers
# ======================================================================================

def test_headers_on_pages_api_csv_and_static(web_settings: Settings, company: Any) -> None:
    with TestClient(create_app(web_settings)) as client:
        for path in ("/", f"/c/{company.id}", "/api/companies", f"/c/{company.id}/leads.csv", "/nope", "/api/nope"):
            resp = client.get(path)
            assert resp.headers["cache-control"] == "no-store", path
            assert resp.headers["cross-origin-opener-policy"] == "same-origin", path
            assert "camera=()" in resp.headers["permissions-policy"], path
            assert "strict-transport-security" not in resp.headers, path
        static = client.get("/static/app.css")
        assert static.status_code == 200 and "no-store" not in static.headers.get("cache-control", "")
        assert static.headers["x-content-type-options"] == "nosniff"


def test_hsts_when_the_dashboard_is_served_over_https(web_settings: Settings) -> None:
    web_settings.base_url = "https://leads.example.com"
    with TestClient(create_app(web_settings), base_url="https://leads.example.com") as client:
        assert client.get("/").headers["strict-transport-security"] == "max-age=31536000"
        assert client.get("/static/app.css").headers["strict-transport-security"] == "max-age=31536000"


def test_an_unexpected_error_page_has_every_header(web_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    web_settings.base_url = "https://leads.example.com"

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(repo, "list_companies", boom)
    with TestClient(create_app(web_settings), base_url="https://leads.example.com",
                    raise_server_exceptions=False) as client:
        for path in ("/companies", "/api/companies"):
            resp = client.get(path)
            assert resp.status_code == 500, path
            for header in ("content-security-policy", "x-content-type-options", "permissions-policy",
                           "cross-origin-opener-policy", "strict-transport-security"):
                assert header in resp.headers, (path, header)
            assert resp.headers["cache-control"] == "no-store"


# ======================================================================================
# 7.7 No personal data in the serve logs
# ======================================================================================

def test_serve_turns_the_access_log_off(settings: Settings, no_uvicorn: list[dict[str, Any]]) -> None:
    cli.main(["serve"])
    assert no_uvicorn[-1]["access_log"] is False


def test_a_lead_search_leaves_no_query_string_in_the_log(tmp_path: Path) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "OPENBERRY_HOME": str(tmp_path), "OPENBERRY_ENV_FILE": str(tmp_path / "none.env"),
           "OPENBERRY_SCHEDULER": "false", "PYTHONPATH": os.pathsep.join(sys.path)}
    server = subprocess.Popen([sys.executable, "-m", "openberry", "serve", "--port", str(port)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace")
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert server.poll() is None and time.monotonic() < deadline, "the server did not start"
            time.sleep(0.1)
        created = httpx.post(f"{base}/api/companies", json={"name": "Log Test Co"}, trust_env=False)
        company_id = created.json()["id"]
        assert httpx.get(f"{base}/c/{company_id}/leads?q=Aisha+Rahman", trust_env=False).status_code == 200
        assert httpx.get(f"{base}/api/companies/{company_id}/leads?search=Aisha", trust_env=False).status_code == 200
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=30)
    assert "Aisha" not in output and "?q=" not in output and "search=" not in output
    assert "Uvicorn running" in output  # the log itself still works


def test_a_mail_server_reply_is_logged_at_debug_level_only(settings: Settings, offline_mail: Any,
                                                          monkeypatch: pytest.MonkeyPatch,
                                                          caplog: pytest.LogCaptureFixture) -> None:
    from email_fakes import FROM, connect_mailbox

    now = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(repo, "EMAIL_LOCAL_TZ", timezone(timedelta(hours=4)))
    monkeypatch.setattr(services, "_sending_stopped", "")
    connect_mailbox(settings)
    repo.save_inbox_check(FROM, True, now=now)
    company = repo.create_company(CompanyIn(name="Acme"))
    repo.update_company(company.id, {"outreach": {"email_sending": True, "email_footer": "Acme, Dubai. Reply "
                                                  "\"unsubscribe\" and we won't email you again."}})
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Pat Doe", email="pat.doe@client.example"))
    repo.create_message(lead.id, "Hi Pat", channel="email", subject="Hello", status="approved", generated_by="claude")
    offline_mail.fail["rcpt"] = (550, b"5.1.1 <pat.doe@client.example>: Recipient address rejected")
    with caplog.at_level(logging.DEBUG, logger="openberry.services"):
        assert services.send_next_email(now=now)["status"] != "sent"
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    quiet = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert loud and not any("pat.doe@client.example" in m for m in loud)
    assert any("pat.doe@client.example" in m for m in quiet)


# ======================================================================================
# 7.8 The OpenAPI schema behind auth in server mode
# ======================================================================================

def test_openapi_schema_in_local_mode_and_server_mode(web_settings: Settings) -> None:
    with TestClient(create_app(web_settings)) as client:
        local = client.get("/api/openapi.json")
        assert local.status_code == 200 and "/api/companies" in local.json()["paths"]
    web_settings.password, web_settings.api_token = STRONG, TOKEN
    with TestClient(create_app(web_settings)) as client:
        assert client.get("/api/openapi.json").status_code == 401
        with_token = client.get("/api/openapi.json", headers={"Authorization": f"Bearer {TOKEN}"})
        assert with_token.status_code == 200 and "/api/companies" in with_token.json()["paths"]
        page = client.get("/login")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        client.post("/login", data={"password": STRONG, "csrf_token": csrf, "next": "/"})
        assert client.get("/api/openapi.json").status_code == 200  # the Connect Claude page links it
