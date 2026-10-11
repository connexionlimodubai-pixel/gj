"""Error pages and the health check (no tracebacks, security headers, plain words for database problems), and the
complete CSV export."""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from openberry import config, db, desktop, repo, scheduler
from openberry.leads_csv import EXPORT_COLUMNS, import_leads_csv
from openberry.models import CompanyIn, LeadIn
from openberry.web import pages
from openberry.web.app import create_app

HUGE = "99999999999999999999999"


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False: the browser's view of an unexpected error, not the test's traceback
    with TestClient(create_app(), base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as c:
        yield c


def a_company(name: str = "Acme Chauffeurs") -> int:
    return repo.create_company(CompanyIn(name=name, website="https://acme.example")).id


def assert_secure(resp: httpx.Response) -> None:
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"


def damage(path: Path) -> None:
    """Overwrite the file's header: SQLite then says "file is not a database"."""
    raw = bytearray(path.read_bytes())
    raw[:100] = b"\x00garbage" * 12 + b"\x00" * 4
    path.write_bytes(bytes(raw))
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Error pages
# --------------------------------------------------------------------------------------


def test_a_damaged_database_gets_a_page_that_says_so(settings, client):
    company_id = a_company()
    damage(settings.db_path)
    for path in (f"/c/{company_id}", "/companies"):
        resp = client.get(path)
        assert resp.status_code == 500
        assert "OpenBerry&#39;s data file is damaged: restore a backup (Your data)." in resp.text
        assert "<h1>OpenBerry can&#39;t read your data</h1>" in resp.text and 'href="/data"' in resp.text
        assert str(settings.db_path.parent) in resp.text and "Traceback" not in resp.text
        assert_secure(resp)
    resp = client.get("/api/companies")
    assert resp.status_code == 500 and resp.json()["detail"].startswith(db.DAMAGED)
    assert client.get("/data").status_code == 200  # Your data still opens: the automatic backups are files


def test_a_full_disk_gets_the_disk_message(client, monkeypatch, settings):
    a_company()

    def full(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(repo, "company_stats", full)
    resp = client.get("/companies")
    assert resp.status_code == 500 and "Your disk is full, so OpenBerry can&#39;t save anything." in resp.text
    assert "<h1>Your disk is full</h1>" in resp.text
    assert f"Your data is in {settings.db_path.parent}" in resp.text and "Details are in" in resp.text
    assert_secure(resp)


def test_a_busy_database_says_try_again(client, monkeypatch):
    a_company()

    def busy(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "company_stats", busy)
    resp = client.get("/companies")
    assert resp.status_code == 503 and "The database is busy, try again in a moment." in resp.text


def test_an_unexpected_error_shows_a_plain_page_without_a_traceback(client, monkeypatch, caplog):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom: internal detail")

    monkeypatch.setattr(pages.repo, "list_companies", broken)
    resp = client.get("/companies")
    assert resp.status_code == 500 and "Something unexpected went wrong. Try again." in resp.text
    assert "boom" not in resp.text and "Traceback" not in resp.text and "RuntimeError" not in resp.text
    assert_secure(resp)  # set on the response: this answer comes from outside the security headers middleware
    assert "boom: internal detail" in caplog.text  # the details go to the log

    monkeypatch.setattr("openberry.web.api.repo.list_companies", broken)
    resp = client.get("/api/companies")
    assert resp.status_code == 500 and "boom" not in resp.text
    assert resp.json()["detail"].startswith("Something unexpected went wrong.")
    assert_secure(resp)


def test_ids_too_large_for_sqlite_are_not_found(client):
    company_id = a_company()
    for path in (f"/c/{HUGE}", f"/c/{HUGE}/leads", f"/c/{company_id}/leads/{HUGE}", f"/c/{HUGE}/settings"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert "There&#39;s no page at this address." in resp.text
        assert_secure(resp)
    for path in (f"/api/companies/{HUGE}", f"/api/leads/{HUGE}"):
        resp = client.get(path)
        assert resp.status_code == 404 and resp.json() == {"detail": "Not found"}


def test_database_problems_are_logged_once(caplog):
    caplog.set_level(logging.DEBUG, logger="openberry.scheduler")
    db.forget_reported()
    for _ in range(3):
        try:
            raise sqlite3.OperationalError("database or disk is full")
        except Exception:
            scheduler._failed("scheduler tick failed")
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "database or disk is full" in errors[0].getMessage()
    for _ in range(2):  # anything else is logged every time
        try:
            raise ValueError("a bug")
        except Exception:
            scheduler._failed("scheduler tick failed")
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 3
    db.forget_reported()
    try:
        raise sqlite3.OperationalError("database or disk is full")
    except Exception:
        scheduler._failed("scheduler tick failed")
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 4  # working again in between: logged
    db.forget_reported()


# --------------------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------------------


def test_healthz_checks_the_database(settings, client):
    a_company()
    assert client.get("/healthz").json() == {"ok": True, "app": "openberry"}
    damage(settings.db_path)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False and body["app"] == "openberry" and "not a database" in body["db"]


def test_the_desktop_probe_still_recognises_an_unhealthy_openberry(monkeypatch):
    answers = {
        "http://127.0.0.1:8001": httpx.Response(503, json={"ok": False, "app": "openberry", "db": "disk is full"}),
        "http://127.0.0.1:8002": httpx.Response(200, json={"ok": True, "app": "openberry"}),
        "http://127.0.0.1:8003": httpx.Response(503, json={"ok": False}),
        "http://127.0.0.1:8004": httpx.Response(200, json={"ok": True}),
    }
    monkeypatch.setattr(httpx, "get", lambda url, **kw: answers[url.removesuffix("/healthz")])
    assert [desktop.answers_like_openberry(url) for url in answers] == [True, True, False, False]


def test_quick_check_runs_once_and_finds_damage(settings):
    a_company()
    assert db.quick_check() == ""
    damage(settings.db_path)
    assert db.quick_check() == ""  # once per process
    db.reset_init_cache()
    assert "not a database" in db.quick_check()


def test_the_desktop_app_shows_restore_steps_for_a_damaged_file(settings, tmp_path, monkeypatch):
    a_company()
    damage(settings.db_path)
    db.reset_init_cache()
    monkeypatch.setattr(config, "OPENBERRY_HOME", tmp_path / "home")
    monkeypatch.setenv("OPENBERRY_DB", str(settings.db_path))
    monkeypatch.setenv("OPENBERRY_SCHEDULER", "false")
    monkeypatch.setattr(desktop, "configure_logging", lambda home: None)
    monkeypatch.setattr(desktop, "start_server", lambda *a, **k: pytest.fail("the server must not start"))
    shown: list[str] = []
    monkeypatch.setattr(desktop, "show_error", lambda message, log_file: shown.append(message))
    assert desktop.run(desktop.parse_args(["--port", "0"])) == 1
    [message] = shown
    assert message.startswith("OpenBerry's data file is damaged") and "openberry-cli restore" in message
    assert str(settings.db_path.parent / "backups") in message


# --------------------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------------------


def bulk_leads(company_id: int, n: int) -> None:
    now = repo.iso()
    with db.connect() as c:
        c.executemany("INSERT INTO leads (company_id, full_name, lead_company, score, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      [(company_id, f"Person {i}", f"Client {i}", i % 100, now, now) for i in range(n)])


def rows_of(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_the_export_has_every_lead_beyond_5000(client):
    company_id = a_company()
    bulk_leads(company_id, 6013)
    resp = client.get(f"/c/{company_id}/leads.csv")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    rows = rows_of(resp.text)
    assert len(rows) == 6013 and len({r["id"] for r in rows}) == 6013
    scores = [int(r["score"]) for r in rows]
    assert scores == sorted(scores, reverse=True)  # the Leads page's order
    assert len(rows_of(client.get(f"/c/{company_id}/leads.csv?q=Person 600").text)) == 1 + 10  # 600, 6000-6009


def test_the_export_has_kind_dates_and_the_ai_rationale(client, company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Lina Noor", lead_company="Gulf Bank",
                                                  linkedin_url="https://www.linkedin.com/in/lina-noor"))
    repo.set_ai_assessment(lead.id, 80, "=Runs travel for 300 staff")
    account, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Northwind", kind="account"))
    text = client.get(f"/c/{company.id}/leads.csv").text
    header = text.splitlines()[0].split(",")
    assert header == EXPORT_COLUMNS
    assert {"kind", "created_at", "updated_at", "ai_rationale"} <= set(header)
    rows = {r["id"]: r for r in rows_of(text)}
    lina, north = rows[str(lead.id)], rows[str(account.id)]
    assert (lina["kind"], north["kind"]) == ("person", "account")
    assert lina["ai_rationale"] == "'=Runs travel for 300 staff"  # formula injection still neutralised
    stored = repo.get_lead(lead.id)
    assert lina["created_at"] == stored.created_at.isoformat() and lina["updated_at"] == stored.updated_at.isoformat()


def test_an_export_reimports_with_the_new_columns(client, company):
    repo.upsert_lead(company.id, LeadIn(full_name="Lina Noor", lead_company="Gulf Bank", email="lina@gulf.example"))
    text = client.get(f"/c/{company.id}/leads.csv").text
    other = a_company("Other Co")
    stats = import_leads_csv(other, text)
    assert stats["created"] == 1 and stats["errors"] == []
    assert repo.list_leads(other)[0][0].email == "lina@gulf.example"


def test_iter_leads_reads_one_snapshot(company):
    for i in range(5):
        repo.upsert_lead(company.id, LeadIn(full_name=f"Person {i}", lead_company=f"Client {i}"))
    pages_seen = repo.iter_leads(company.id, page_size=2)
    first = next(pages_seen)
    writer = sqlite3.connect(config.get_settings().db_path)
    writer.execute("INSERT INTO leads (company_id, full_name, score, created_at, updated_at) "
                   "VALUES (?, 'Added during the export', 100, ?, ?)", (company.id, repo.iso(), repo.iso()))
    writer.commit()
    writer.close()
    rest = [lead for page in pages_seen for lead in page]
    names = [lead.full_name for lead in [*first, *rest]]
    assert len(names) == 5 and len(set(names)) == 5 and "Added during the export" not in names
    assert len(repo.list_leads(company.id)[0]) == 6


def test_parsed_forms_are_closed(client, company, monkeypatch):
    closed: list[bool] = []
    real_close = FormData.close

    async def close(self: FormData) -> None:
        closed.append(True)
        await real_close(self)

    monkeypatch.setattr(FormData, "close", close)
    page = client.get(f"/c/{company.id}/settings").text
    token = page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    client.post(f"/c/{company.id}/status", data={"csrf_token": token, "status": "paused"})
    assert closed
    closed.clear()
    assert client.post(f"/c/{company.id}/status", data={"csrf_token": "wrong"}).status_code == 403
    assert closed  # refused forms too
