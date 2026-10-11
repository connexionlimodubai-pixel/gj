"""Backups and safe upgrades: db.backup_to, snapshots (daily, before an upgrade, before Delete company), the too-new
guard, `openberry backup` and `openberry restore`, and the Your data page."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from email_fakes import FROM, connect_mailbox
from openberry import backups, cli, config, db, desktop, repo, scheduler, services
from openberry.mcp_server import build_server
from openberry.models import CompanyIn, LeadIn
from openberry.web.app import create_app

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
COUNTED = ("companies", "leads", "lead_keys", "messages", "scan_runs", "email_suppressions")


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as c:
        yield c


def csrf(client: TestClient, path: str = "/data") -> str:
    page = client.get(path).text
    return page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]


def counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTED}


def version_of(path: Path) -> int:
    with closing(sqlite3.connect(path)) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def integrity(path: Path) -> str:
    with closing(sqlite3.connect(path)) as c:
        return c.execute("PRAGMA integrity_check").fetchone()[0]


def set_version(path: Path, version: int) -> None:
    with closing(sqlite3.connect(path)) as c:
        c.execute(f"PRAGMA user_version={version}")
        c.commit()


def a_company(name: str = "Acme Chauffeurs", leads: int = 3) -> int:
    company = repo.create_company(CompanyIn(name=name, website="https://acme.example"))
    for i in range(leads):
        repo.upsert_lead(company.id, LeadIn(full_name=f"Person {i}", lead_company=f"Client {i}",
                                            linkedin_url=f"https://www.linkedin.com/in/person-{name[:4]}-{i}"))
    return company.id


def snaps(kind: str | None = None) -> list[backups.Snapshot]:
    return [s for s in backups.list_snapshots() if kind is None or s.kind == kind]


# --------------------------------------------------------------------------------------
# db.backup_to: consistent, with what is still in the -wal file
# --------------------------------------------------------------------------------------


def test_a_backup_holds_rows_still_in_the_wal_while_another_connection_is_open(settings, tmp_path):
    company_id = a_company(leads=0)
    live = settings.db_path
    holder = sqlite3.connect(live)  # another process's connection: the last close would checkpoint the -wal
    holder.execute("SELECT COUNT(*) FROM companies").fetchone()
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA wal_autocheckpoint=0")  # the rows stay in the -wal file
    now = repo.iso()
    writer.executemany("INSERT INTO leads (company_id, full_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                       [(company_id, f"WAL lead {i}", now, now) for i in range(500)])
    writer.commit()
    try:
        assert live.with_name(live.name + "-wal").stat().st_size > 0
        plain = tmp_path / "plain-copy.db"
        shutil.copyfile(live, plain)  # what "copy openberry.db" did: the -wal's rows are missing
        assert counts(plain)["leads"] < 500

        copy = db.backup_to(tmp_path / "out" / "backup.db")
        assert counts(copy)["leads"] == 500 and integrity(copy) == "ok"
        assert version_of(copy) == db.SCHEMA_VERSION
        assert not list((tmp_path / "out").glob("*.tmp"))  # the temporary name is gone
    finally:
        writer.close()
        holder.close()


def test_a_failed_backup_leaves_no_partial_file(settings, tmp_path, monkeypatch):
    a_company()

    class Broken(sqlite3.Connection):
        def backup(self, *args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("database or disk is full")

    real_connect = sqlite3.connect
    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **k: real_connect(*a, **k, factory=Broken))
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        db.backup_to(tmp_path / "out" / "backup.db")
    assert list((tmp_path / "out").iterdir()) == []


def test_backing_up_a_missing_database_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        db.backup_to(tmp_path / "b.db", tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists() and not (tmp_path / "b.db").exists()


# --------------------------------------------------------------------------------------
# Before a schema upgrade, and a database a newer version wrote
# --------------------------------------------------------------------------------------


def test_an_older_database_is_copied_before_it_is_upgraded(settings):
    a_company()
    path = settings.db_path
    before = counts(path)
    set_version(path, 3)
    db.reset_init_cache()
    db.init_db(path)
    [copy] = snaps(backups.PRE_UPGRADE)
    assert copy.name.startswith("openberry-pre-v3-") and copy.version == 3 and copy.label == "Before an update (database v3)"
    assert version_of(copy.path) == 3 and counts(copy.path) == before and integrity(copy.path) == "ok"
    assert version_of(path) == db.SCHEMA_VERSION
    db.init_db(path)  # opening it again: already up to date, no new copy
    assert len(snaps(backups.PRE_UPGRADE)) == 1


def test_a_new_database_gets_no_copy(settings):
    db.init_db(settings.db_path)
    assert version_of(settings.db_path) == db.SCHEMA_VERSION and snaps() == []
    assert not backups.folder().exists()


def test_the_last_three_upgrade_copies_are_kept(settings):
    a_company()
    for day in range(5):
        backups.snapshot(backups.PRE_UPGRADE, now=T0 + timedelta(days=day), version=3)
    assert [s.created for s in snaps(backups.PRE_UPGRADE)] == [T0 + timedelta(days=d) for d in (4, 3, 2)]


def test_an_upgrade_whose_copy_fails_changes_nothing(settings, monkeypatch):
    a_company()
    path = settings.db_path
    set_version(path, 3)
    db.reset_init_cache()
    backups.folder().parent.mkdir(parents=True, exist_ok=True)
    backups.folder().write_text("not a folder")  # backups/ can't be created (as with a read-only data folder)
    with pytest.raises(db.UpgradeBackupFailed, match="could not save a copy of it first") as exc:
        db.init_db(path)
    assert "Nothing was changed" in str(exc.value) and str(backups.folder()) in str(exc.value)
    assert version_of(path) == 3
    with pytest.raises(db.UpgradeBackupFailed):  # every connection refuses, and tries again
        repo.list_companies()

    backups.folder().unlink()

    def disk_full(dest: Path, source: Path | None = None) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(db, "backup_to", disk_full)
    with pytest.raises(db.UpgradeBackupFailed, match="No space left on device"):
        db.init_db(path)
    assert version_of(path) == 3
    monkeypatch.undo()
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"]  # once it can, it upgrades
    assert version_of(path) == db.SCHEMA_VERSION and len(snaps(backups.PRE_UPGRADE)) == 1


TOO_NEW = "This data was saved by a newer OpenBerry (database v{}). Open the newer app, or restore a backup."


@pytest.fixture
def too_new(settings) -> int:
    a_company()
    version = db.SCHEMA_VERSION + 1
    set_version(settings.db_path, version)
    db.reset_init_cache()
    return version


def test_a_database_from_a_newer_version_is_refused_and_left_as_it_is(settings, too_new):
    with pytest.raises(db.DatabaseTooNew) as exc:
        db.init_db(settings.db_path)
    assert str(exc.value) == TOO_NEW.format(too_new) and exc.value.version == too_new
    assert version_of(settings.db_path) == too_new and snaps() == []
    with pytest.raises(db.DatabaseTooNew):
        repo.list_companies()
    assert version_of(settings.db_path) == too_new


def test_the_command_line_says_so_and_exits_1(too_new, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["rescore"])
    assert exc.value.code == 1
    assert TOO_NEW.format(too_new) in capsys.readouterr().err


async def test_claude_gets_it_as_a_tool_error(too_new):
    async with Client(build_server()) as client:
        result = await client.call_tool("list_companies", {})
    assert result.is_error and TOO_NEW.format(too_new) in result.content[0].text


def test_the_dashboard_shows_it_on_its_error_page(client, too_new):
    for path in ("/", "/companies", "/c/1"):
        resp = client.get(path)
        assert resp.status_code == 503 and TOO_NEW.format(too_new) in resp.text
        assert "can&#39;t open your data" in resp.text and resp.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'self'" in resp.headers["content-security-policy"]
    resp = client.get("/api/companies")
    assert resp.status_code == 503 and resp.json() == {"detail": TOO_NEW.format(too_new)}
    resp = client.get("/healthz")
    assert resp.status_code == 503 and resp.json() == {"ok": False, "app": "openberry", "db": TOO_NEW.format(too_new)}
    # Your data still opens, and its backups can still be downloaded: the file is backed up as it is.
    assert client.get("/data").status_code == 200
    copy = client.get("/data/backup")
    assert copy.status_code == 200 and copy.content.startswith(b"SQLite format 3")


def test_the_desktop_app_shows_it_in_a_window(settings, too_new, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OPENBERRY_HOME", tmp_path / "home")
    monkeypatch.setenv("OPENBERRY_DB", str(settings.db_path))
    monkeypatch.setenv("OPENBERRY_SCHEDULER", "false")
    monkeypatch.setattr(desktop, "configure_logging", lambda home: None)
    monkeypatch.setattr(desktop, "start_server", lambda *a, **k: pytest.fail("the server must not start"))
    shown: list[str] = []
    monkeypatch.setattr(desktop, "show_error", lambda message, log_file: shown.append(message))
    assert desktop.run(desktop.parse_args(["--port", "0"])) == 1
    assert shown == [TOO_NEW.format(too_new)]
    assert version_of(settings.db_path) == too_new


# --------------------------------------------------------------------------------------
# Daily snapshots
# --------------------------------------------------------------------------------------


def test_daily_snapshots_once_a_day_keep_7_days_and_4_weeks(settings):
    a_company()
    made = []
    for day in range(60):
        now = T0 + timedelta(days=day)
        made.append(backups.daily_snapshot(now=now))
        assert backups.daily_snapshot(now=now + timedelta(hours=6)) is None  # once a day
        kept = snaps(backups.DAILY)
        assert len(kept) == min(day + 1, 7 + 4) if day < 7 else len(kept) <= 11
    assert all(made)
    kept = snaps(backups.DAILY)
    assert len(kept) == 11
    days = [s.created.date() for s in kept]
    assert days[:7] == [(T0 + timedelta(days=59 - d)).date() for d in range(7)]  # the last 7 days
    weeks = [s.created.isocalendar()[:2] for s in kept[7:]]
    assert len(set(weeks)) == 4 and all(s.created < kept[6].created for s in kept[7:])  # 4 weeks before them
    assert all(integrity(s.path) == "ok" for s in kept)


def test_a_clock_that_went_back_still_gets_a_daily_snapshot(settings):
    a_company()
    assert backups.daily_snapshot(now=T0 + timedelta(hours=4))  # the clock was 4 hours ahead
    assert backups.daily_snapshot(now=T0) is not None  # back to the right time: the future one doesn't count
    assert backups.daily_snapshot(now=T0 + timedelta(minutes=10)) is None


def test_copies_the_user_made_and_other_files_are_never_pruned(settings):
    a_company()
    mine = backups.manual_backup(now=T0)
    other = backups.folder() / "notes.txt"
    other.write_text("keep me")
    for day in range(30):
        backups.daily_snapshot(now=T0 + timedelta(days=day))
    assert mine.exists() and other.exists() and len(snaps(backups.MANUAL)) == 1


async def test_the_scheduler_saves_the_daily_snapshot(settings, monkeypatch):
    a_company()

    async def nothing(*args: Any) -> Any:
        return {}

    monkeypatch.setattr(scheduler, "scan_due_companies", lambda: nothing())
    monkeypatch.setattr(scheduler, "alert_active_companies", lambda: nothing())
    monkeypatch.setattr(scheduler, "auto_approve_active_companies", lambda: nothing())
    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.scheduler_loop(stop))
    for _ in range(300):
        if snaps(backups.DAILY):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert len(snaps(backups.DAILY)) == 1


async def test_a_failing_daily_snapshot_doesnt_stop_the_scans(settings, monkeypatch, caplog):
    def broken(now: datetime | None = None) -> None:
        raise OSError("No space left on device")

    scanned = asyncio.Event()

    async def scans() -> list[dict[str, Any]]:
        scanned.set()
        return []

    monkeypatch.setattr(backups, "daily_snapshot", broken)
    monkeypatch.setattr(scheduler, "scan_due_companies", scans)
    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.scheduler_loop(stop))
    await asyncio.wait_for(scanned.wait(), 5)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert "daily backup failed" in caplog.text


# --------------------------------------------------------------------------------------
# Delete company keeps a copy first
# --------------------------------------------------------------------------------------


def test_delete_company_saves_a_backup_first(client, settings):
    company_id = a_company()
    other = a_company("Other Co", leads=1)
    token = csrf(client, f"/c/{company_id}/settings")
    resp = client.post(f"/c/{company_id}/delete", data={"csrf_token": token}, follow_redirects=True)
    assert resp.status_code == 200 and "A backup of your data was saved first" in resp.text
    [copy] = snaps(backups.BEFORE_DELETE)
    assert copy.path.parent == settings.db_path.parent / "backups"
    with closing(sqlite3.connect(copy.path)) as c:
        assert {r[0] for r in c.execute("SELECT name FROM companies")} == {"Acme Chauffeurs", "Other Co"}
    assert [c.id for c in repo.list_companies()] == [other]


def test_delete_company_deletes_nothing_without_its_backup(client, monkeypatch):
    company_id = a_company()

    def disk_full(dest: Path, source: Path | None = None) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(db, "backup_to", disk_full)
    token = csrf(client, f"/c/{company_id}/settings")
    resp = client.post(f"/c/{company_id}/delete", data={"csrf_token": token}, follow_redirects=True)
    assert "Nothing was deleted: OpenBerry could not save a backup of your data first" in resp.text
    assert [c.id for c in repo.list_companies()] == [company_id] and len(repo.list_leads(company_id)[0]) == 3


def test_ten_delete_backups_are_kept(settings):
    a_company()
    for minute in range(12):
        backups.snapshot(backups.BEFORE_DELETE, now=T0 + timedelta(minutes=minute))
    kept = snaps(backups.BEFORE_DELETE)
    assert len(kept) == 10 and kept[-1].created == T0 + timedelta(minutes=2)


def test_two_snapshots_in_the_same_second_both_stay(settings):
    a_company()
    first = backups.snapshot(backups.BEFORE_DELETE, now=T0)
    second = backups.snapshot(backups.BEFORE_DELETE, now=T0)
    assert first != second and first.exists() and second.exists() and second.name.endswith("-2-before-delete.db")
    assert [s.path for s in snaps()] == [second, first]


# --------------------------------------------------------------------------------------
# openberry backup
# --------------------------------------------------------------------------------------


def test_openberry_backup_saves_a_copy(settings, tmp_path, capsys):
    a_company()
    cli.main(["backup"])
    [copy] = snaps(backups.MANUAL)
    assert f"Backup saved: {copy.path}" in capsys.readouterr().out
    assert counts(copy.path) == counts(settings.db_path)

    target = tmp_path / "usb" / "mine.db"
    cli.main(["backup", str(target)])
    assert counts(target) == counts(settings.db_path) and f"Backup saved: {target}" in capsys.readouterr().out
    folder = tmp_path / "usb"
    cli.main(["backup", str(folder)])
    out = capsys.readouterr().out
    assert len(list(folder.glob("openberry-*.db"))) == 1 and str(folder) in out


def test_openberry_backup_without_data_says_so(settings, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["backup"])
    assert "there is no data to back up yet" in str(exc.value.code)
    assert not settings.db_path.exists()


def test_the_app_executable_runs_backup_and_restore(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "main", calls.append)
    monkeypatch.setattr(desktop, "run", lambda *a, **k: pytest.fail("the app must not start"))
    desktop.main(["backup"])
    desktop.main(["restore", "x.db"])
    assert calls == [["backup"], ["restore", "x.db"]]
    assert {"backup", "restore"} <= desktop.CLI_COMMANDS


# --------------------------------------------------------------------------------------
# openberry restore
# --------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)  # Monday 10:00 in Dubai
FOOTER = ('Acme Chauffeurs · Office 1204, Bay Square, Business Bay, Dubai\n'
          'Not interested? Reply "unsubscribe" and we won\'t email you again.')


@pytest.fixture
def mailbox(settings, offline_mail, monkeypatch):
    monkeypatch.setattr(repo, "EMAIL_LOCAL_TZ", timezone(timedelta(hours=4)))
    monkeypatch.setattr(services, "_sending_stopped", "")
    connect_mailbox(settings)
    repo.save_inbox_check(FROM, True, now=NOW)
    return offline_mail


def test_restore_puts_the_backup_back_and_turns_sending_off(settings, mailbox, tmp_path, capsys):
    company = repo.create_company(CompanyIn(name="Acme Chauffeurs"))
    repo.update_company(company.id, {"outreach": {"email_sending": True, "agent_sending": True, "email_footer": FOOTER}})
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Lina Noor", email="lina@gulfbank.example"))
    msg = repo.create_message(lead.id, "Hello Lina", channel="email", subject="Hi", status="approved",
                              generated_by="claude")
    backup = db.backup_to(tmp_path / "Downloads" / "openberry-backup-2026-10-12.db")

    assert services.send_next_email(now=NOW)["status"] == "sent"  # sent after the backup was made
    assert repo.get_message(msg.id).status == "sent" and len(mailbox.sent) == 1
    live = settings.db_path
    holder = sqlite3.connect(live)  # an idle connection keeps the -wal file, with what came after the backup
    holder.execute("SELECT COUNT(*) FROM companies").fetchone()
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO companies (name, created_at, updated_at) VALUES ('After the backup', ?, ?)",
                   (repo.iso(), repo.iso()))
    writer.commit()
    writer.close()
    assert live.with_name(live.name + "-wal").stat().st_size > 0

    if sys.platform == "win32":  # Windows can't delete or replace a file another program has open: nothing changes
        with pytest.raises(backups.RestoreRefused, match="Another program is using your data"):
            backups.restore(backup, app_running=lambda: False)
        with closing(sqlite3.connect(live)) as c:
            assert c.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2
        holder.close()
    cli_done = backups.restore(backup, app_running=lambda: False)
    holder.close()
    restored = repo.get_message(msg.id)
    assert (restored.status, restored.sent_at, restored.email_state) == ("approved", None, "")  # as in the backup
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"]  # the -wal wasn't replayed into it
    outreach = repo.get_company(company.id).outreach
    assert outreach.email_sending is False and outreach.agent_sending is False
    assert cli_done.sending_turned_off == ["Acme Chauffeurs"] and cli_done.version == db.SCHEMA_VERSION
    assert services.send_next_email(now=NOW + timedelta(minutes=30))["status"] != "sent"  # never sent twice
    assert len(mailbox.sent) == 1
    # The data from before the restore is kept, sent email and all.
    assert cli_done.previous is not None and cli_done.previous.name.endswith("-before-restore.db")
    with closing(sqlite3.connect(cli_done.previous)) as c:
        assert c.execute("SELECT status FROM messages WHERE id = ?", (msg.id,)).fetchone()[0] == "sent"
        assert c.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2
    assert backup.exists() and integrity(live) == "ok"  # the user's file stays where it was


def test_restore_from_the_command_line_prints_what_it_did(settings, tmp_path, monkeypatch, capsys):
    company_id = a_company()
    repo.update_company(company_id, {"outreach": {"agent_sending": True}})
    backup = db.backup_to(tmp_path / "b.db")
    monkeypatch.setattr(backups, "_app_running", lambda: False)
    cli.main(["restore", str(backup)])
    out = capsys.readouterr().out
    assert f"Restored {backup}." in out and "saved first" in out
    assert "Email sending and AI agent sending are now off for: Acme Chauffeurs." in out
    assert repo.get_company(company_id).outreach.agent_sending is False


def test_restore_upgrades_an_older_backup(settings, tmp_path):
    a_company()
    backup = db.backup_to(tmp_path / "old.db")
    set_version(backup, 3)
    done = backups.restore(backup, app_running=lambda: False)
    assert done.version == 3 and version_of(settings.db_path) == db.SCHEMA_VERSION
    assert len(snaps(backups.PRE_UPGRADE)) == 1 and version_of(backup) == 3


@pytest.mark.parametrize("problem", ["missing", "damaged", "not_openberry", "too_new", "same_file"])
def test_restore_refuses_a_bad_backup_and_changes_nothing(settings, tmp_path, problem):
    a_company()
    before = counts(settings.db_path)
    backup = tmp_path / "backup.db"
    if problem == "damaged":
        backup.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200 + b"garbage" * 500)
    elif problem == "not_openberry":
        with closing(sqlite3.connect(backup)) as c:
            c.execute("CREATE TABLE notes (x)")
    elif problem == "too_new":
        db.backup_to(backup)
        set_version(backup, db.SCHEMA_VERSION + 1)
    elif problem == "same_file":
        backup = settings.db_path
    expected = {"missing": "There is no file at", "damaged": "damaged|not an OpenBerry backup", "not_openberry": "is not an OpenBerry backup",
                "too_new": "was saved by a newer OpenBerry", "same_file": "is the data file OpenBerry uses now"}
    with pytest.raises(backups.RestoreRefused, match=expected[problem]):
        backups.restore(backup, app_running=lambda: False)
    assert counts(settings.db_path) == before and snaps() == []


def test_restore_refuses_while_the_app_is_open(settings, tmp_path, monkeypatch):
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    home = tmp_path / "home"
    monkeypatch.setattr(config, "OPENBERRY_HOME", home)
    lock = desktop.InstanceLock(home / desktop.LOCK_FILE)
    assert lock.acquire()
    try:
        with pytest.raises(backups.RestoreRefused, match="OpenBerry is open. Close OpenBerry and quit Claude"):
            backups.restore(backup)
    finally:
        lock.release()
    assert snaps() == []
    assert backups.restore(backup).previous is not None  # closed: it works


def test_restore_refuses_while_another_program_writes(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "RESTORE_LOCK_TIMEOUT", 0.2)
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    writer = sqlite3.connect(settings.db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(backups.RestoreRefused, match="Another program is using your data"):
            backups.restore(backup, app_running=lambda: False)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"]


def test_restore_replaces_a_damaged_file_and_keeps_it(settings, tmp_path):
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    live = settings.db_path
    raw = bytearray(live.read_bytes())
    raw[:100] = b"\x00" * 100  # "file is not a database"
    live.write_bytes(bytes(raw))
    done = backups.restore(backup, app_running=lambda: False)
    assert done.previous is not None and done.previous.read_bytes()[:100] == b"\x00" * 100  # kept as it was
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"] and integrity(live) == "ok"


def test_restore_refused_on_the_command_line_exits_with_the_reason(settings, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore", str(tmp_path / "nope.db")])
    assert "There is no file at" in str(exc.value.code) and "Nothing was changed." in str(exc.value.code)


# --------------------------------------------------------------------------------------
# The Your data page
# --------------------------------------------------------------------------------------


def test_the_your_data_page(client, settings):
    a_company()
    page = client.get("/data")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    text = page.text
    assert "Your data" in text and 'href="/data" aria-current="page"' in text
    assert str(settings.db_path.parent) in text and 'href="/data/backup"' in text
    assert "No automatic backups yet." not in text or not snaps()
    assert "Restore a backup" in text and "email sending and AI agent sending are off" in text

    backups.daily_snapshot()
    backups.snapshot(backups.PRE_UPGRADE, version=3)
    text = client.get("/data").text
    for s in snaps():
        assert f'href="/data/backups/{s.name}"' in text
    assert "Daily" in text and "Before an update (database v3)" in text


def test_download_backup_is_a_whole_consistent_copy(client, settings, tmp_path):
    a_company()
    a_company("Other Co", leads=2)
    resp = client.get("/data/backup")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/vnd.sqlite3"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f'filename="openberry-backup-{today}.db"' in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "no-store"
    copy = tmp_path / "downloaded.db"
    copy.write_bytes(resp.content)
    assert counts(copy) == counts(settings.db_path) and integrity(copy) == "ok"
    leftovers = list(settings.db_path.parent.glob(".download-*"))
    assert leftovers == []  # the temporary copy is deleted once sent


def test_automatic_backups_can_be_downloaded_by_name_only(client, settings):
    a_company()
    snap = backups.snapshot(backups.BEFORE_DELETE)
    resp = client.get(f"/data/backups/{snap.name}")
    assert resp.status_code == 200 and resp.content == snap.read_bytes()
    for name in ("openberry-20990101-000000.db", "..%2Ftest.db", "test.db", "notes.txt"):
        assert client.get(f"/data/backups/{name}").status_code == 404


def test_the_your_data_page_needs_the_login_in_server_mode(settings):
    settings.password = "a long password here"
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as c:
        for path in ("/data", "/data/backup", "/data/backups/openberry-20261001-090000-daily.db"):
            resp = c.get(path, follow_redirects=False)
            assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_the_restore_command_on_the_page_matches_the_install(client, settings, monkeypatch):
    from openberry.web import data as data_page

    monkeypatch.setattr(data_page, "mcp_launch", lambda path: {
        "command": "C:\\Apps\\OpenBerry\\openberry-cli.exe", "args": ["mcp"], "env": {}, "docker": False})
    text = client.get("/data").text
    assert '&#34;C:\\Apps\\OpenBerry\\openberry-cli.exe&#34; restore openberry-backup-' in text
    monkeypatch.setattr(data_page, "mcp_launch", lambda path: {
        "command": "docker", "args": ["exec", "-i", "openberry", "openberry", "mcp"], "env": {}, "docker": True})
    text = client.get("/data").text
    assert "docker compose stop openberry" in text and "openberry restore /data/backups/" in text


def test_concurrent_upgrades_both_succeed(settings):
    """Two processes opening an older file at once: both wait for the upgrade, and a copy exists."""
    a_company()
    set_version(settings.db_path, 3)
    db.reset_init_cache()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def open_it() -> None:
        try:
            barrier.wait()
            db.init_db(settings.db_path)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == [] and version_of(settings.db_path) == db.SCHEMA_VERSION
    assert 1 <= len(snaps(backups.PRE_UPGRADE)) <= 2
    assert all(version_of(s.path) in (3, db.SCHEMA_VERSION) for s in snaps(backups.PRE_UPGRADE))

