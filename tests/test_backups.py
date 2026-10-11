"""Backups and safe upgrades: db.backup_to, snapshots (daily, before an upgrade, before Delete company), the too-new
guard, `openberry backup` and `openberry restore`, and the Your data page."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
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


def test_openberry_backup_to_a_folder_not_made_yet_makes_it(settings, tmp_path):
    """DOC-3: `backup D:\\Backups` before the folder exists made a file called Backups, replaced by every later run."""
    a_company()
    folder = tmp_path / "Backups"
    cli.main(["backup", str(folder)])
    cli.main(["backup", str(folder)])  # again: a second copy, even within the same second
    copies = sorted(folder.glob("openberry-*.db"))
    assert folder.is_dir() and len(copies) == 2 and all(counts(c) == counts(settings.db_path) for c in copies)
    if sys.platform != "win32":
        assert folder.stat().st_mode & 0o777 == 0o700  # made private


def test_openberry_backup_never_replaces_a_file_unless_told_to(settings, tmp_path):
    a_company()
    target = tmp_path / "mine.db"
    cli.main(["backup", str(target)])
    before = target.read_bytes()
    a_company("Other Co")
    with pytest.raises(SystemExit) as exc:
        cli.main(["backup", str(target)])
    said = str(exc.value.code)
    assert f"There is already a file at {target}." in said and "Nothing was saved." in said
    assert target.read_bytes() == before
    cli.main(["backup", str(target), "--overwrite"])
    assert counts(target) == counts(settings.db_path)


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
    restored = repo.get_message(msg.id)  # approved in the backup: kept as sent from the data it replaced
    assert (restored.status, restored.email_state, restored.sent_via) == ("sent", "sent", "smtp")
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
    assert ("The never-email list and the record of sent emails were kept from it. So were the opt-outs, replies and "
            "sends it recorded for the leads and messages in the backup.") in out
    assert "Email sending and AI agent sending are now off for: Acme Chauffeurs." in out
    assert ("Leads and messages added after this backup was made are not in it: before you turn sending back on, "
            "check LinkedIn and your mailbox's Sent folder.") in out
    assert repo.get_company(company_id).outreach.agent_sending is False


def test_restore_upgrades_an_older_backup(settings, tmp_path):
    a_company()
    backup = db.backup_to(tmp_path / "old.db")
    set_version(backup, 3)
    done = backups.restore(backup, app_running=lambda: False)
    assert done.version == 3 and version_of(settings.db_path) == db.SCHEMA_VERSION
    # Upgraded in the copy, before the swap: the backup file, untouched, is its pre-upgrade copy.
    assert snaps(backups.PRE_UPGRADE) == [] and version_of(backup) == 3


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
        with pytest.raises(backups.RestoreRefused,
                           match=r"OpenBerry is open\. Close OpenBerry \(or stop openberry serve\) and quit Claude"):
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


def test_restore_replaces_a_damaged_file_and_keeps_it(settings, tmp_path, monkeypatch, capsys, caplog):
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    live = settings.db_path
    raw = bytearray(live.read_bytes())
    raw[:100] = b"\x00" * 100  # "file is not a database"
    live.write_bytes(bytes(raw))
    monkeypatch.setattr(backups, "_app_running", lambda: False)
    cli.main(["restore", str(backup)])
    [previous] = snaps(backups.BEFORE_RESTORE)
    assert previous.path.read_bytes() == bytes(raw)  # kept as it was
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"] and integrity(live) == "ok"
    # Its opt-outs and sent emails can't be read: the restore goes on, and says so.
    assert "were not kept" in capsys.readouterr().out and "are not kept" in caplog.text
    assert [p.name for p in backups.folder().iterdir()] == [previous.name]


def test_restore_replaces_a_file_with_a_damaged_table(settings, tmp_path, monkeypatch, capsys):
    """The usual damage: the file opens, but a table in it is malformed, and the dashboard says to restore a backup.
    Reading the email history from it failed, and that stopped the restore: nothing could be restored."""
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    live = settings.db_path
    with closing(sqlite3.connect(live)) as c:
        root = c.execute("SELECT rootpage FROM sqlite_master WHERE name = 'messages'").fetchone()[0]
        size = c.execute("PRAGMA page_size").fetchone()[0]
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = bytearray(live.read_bytes())
    raw[(root - 1) * size] = 0xFF  # not a kind of page SQLite knows: "database disk image is malformed"
    live.write_bytes(bytes(raw))
    monkeypatch.setattr(backups, "_app_running", lambda: False)
    cli.main(["restore", str(backup)])
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"] and integrity(live) == "ok"
    assert "were not kept" in capsys.readouterr().out


def test_a_backup_path_that_cant_be_read_is_refused_and_nothing_changes(settings, tmp_path, monkeypatch):
    """An error reading the backup's path (a name too long here; a network drive that is offline, say) came out of
    restore() before the copy, and `openberry restore` said the backup was in place."""
    a_company()
    before = counts(settings.db_path)
    monkeypatch.setattr(backups, "_app_running", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore", str(tmp_path / ("b" * 300 + ".db"))])
    said = str(exc.value.code)
    assert said.startswith("openberry restore: ") and said.endswith(" Nothing was changed.")
    assert "in place" not in said
    assert counts(settings.db_path) == before and snaps() == []


def test_an_interruption_right_after_the_swap_leaves_sending_off(settings, tmp_path, monkeypatch):
    """UPG-1: sending was turned off after the swap, so Ctrl-C or a power cut just after it left the backup in place
    with sending on. Now the copy is ready (upgraded, sending off) before it takes the live file's place."""
    company_id = a_company()
    repo.update_company(company_id, {"outreach": {"email_sending": True, "agent_sending": True,
                                                  "email_footer": FOOTER}})
    backup = db.backup_to(tmp_path / "b.db")
    set_version(backup, 3)  # an older backup: upgraded before the swap too
    live = settings.db_path
    replace = os.replace

    def interrupted(src: Any, dst: Any) -> None:
        replace(src, dst)
        if Path(dst) == live:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        backups.restore(backup, app_running=lambda: False)
    with closing(sqlite3.connect(live)) as c:
        outreach = json.loads(c.execute("SELECT outreach FROM companies").fetchone()[0])
        version = c.execute("PRAGMA user_version").fetchone()[0]
    assert outreach["email_sending"] is False and outreach["agent_sending"] is False
    assert version == db.SCHEMA_VERSION


def test_a_restore_that_fails_before_the_swap_changes_nothing_and_says_so(settings, tmp_path, monkeypatch):
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    a_company("Made after the backup")
    before = counts(settings.db_path)
    monkeypatch.setattr(backups, "_app_running", lambda: False)

    def disk_full(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(db, "init_db", disk_full)
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore", str(backup)])
    assert str(exc.value.code) == ("openberry restore: The restore stopped before your data was replaced (database or "
                                   "disk is full). Nothing was changed.")
    assert counts(settings.db_path) == before
    assert not list(settings.db_path.parent.glob(f".{settings.db_path.name}.restore-*"))  # its copy is gone


def test_an_error_after_the_swap_says_the_backup_is_in_place_with_sending_off(settings, tmp_path, monkeypatch):
    company_id = a_company()
    repo.update_company(company_id, {"outreach": {"email_sending": True, "agent_sending": True,
                                                  "email_footer": FOOTER}})
    backup = db.backup_to(tmp_path / "b.db")
    monkeypatch.setattr(backups, "_app_running", lambda: False)
    reset = db.reset_init_cache

    def fails_once() -> None:  # after the swap: nothing that matters is left to do then
        monkeypatch.setattr(db, "reset_init_cache", reset)
        raise OSError("something unexpected")

    monkeypatch.setattr(db, "reset_init_cache", fails_once)
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore", str(backup)])
    assert str(exc.value.code) == ("openberry restore: the backup is now in place, and email sending and AI agent "
                                   "sending are off for every company. Something failed after that: something "
                                   "unexpected")
    db.reset_init_cache()
    outreach = repo.get_company(company_id).outreach
    assert outreach.email_sending is False and outreach.agent_sending is False


def test_restore_keeps_opt_outs_and_never_email_entries_made_after_the_backup(settings):
    """UPG-2: Jane phoned to say "don't email me" after the daily backup. Restoring that backup brought her approved
    email back, without the opt-out."""
    company_id = a_company(leads=0)
    jane, _ = repo.upsert_lead(company_id, LeadIn(full_name="Jane Roe", lead_company="X", email="jane@x.example"))
    msg = repo.create_message(jane.id, "Hi Jane", channel="email", subject="Hello", status="approved")
    snap = backups.snapshot(backups.DAILY)
    repo.record_opt_out(jane.id, "phone call")
    late, _ = repo.upsert_lead(company_id, LeadIn(full_name="Omar Late", email="omar@y.example"))  # not in the backup
    repo.record_opt_out(late.id, "LinkedIn message")
    repo.add_suppression("ops@z.example", "manual", "asked by phone")
    done = backups.restore(snap, app_running=lambda: False)
    assert repo.email_queue(company_id)["items"] == []
    assert repo.get_message(msg.id).status == "draft"  # her opt-out is kept on her lead too: the approval lapsed
    kept = {s["email"]: s for s in repo.list_suppressions()}
    assert set(kept) == {"jane@x.example", "omar@y.example", "ops@z.example"}
    assert kept["jane@x.example"]["lead_id"] == jane.id  # the backup has that lead
    assert kept["omar@y.example"]["lead_id"] is None and kept["omar@y.example"]["company_id"] == company_id
    assert done.kept_email_history


def test_restore_keeps_the_record_of_emails_sent_after_the_backup(settings, mailbox, tmp_path):
    """EMAIL-1: after the backup, Lina's email went out, Omar opted out and Rania's email was cut off in DATA. The
    backup has all three approved and unsent: none goes out when sending is turned back on."""
    company = repo.create_company(CompanyIn(name="Acme Chauffeurs"))
    repo.update_company(company.id, {"outreach": {"email_sending": True, "email_footer": FOOTER}})
    ids = {}
    for name in ("sami", "lina", "omar", "rania"):
        lead, _ = repo.upsert_lead(company.id, LeadIn(full_name=name.title(), email=f"{name}@gulfbank.example"))
        ids[name] = repo.create_message(lead.id, f"Hello {name.title()}", channel="email", subject="Hi",
                                        status="approved", generated_by="claude").id
    with db.connect() as c:  # Sami's email went out before the backup: it is in it
        c.execute("UPDATE messages SET status = 'sent', sent_at = ?, sent_via = 'smtp', email_state = 'sent', "
                  "email_message_id = '<sami-1@acme-mail.example>', email_to = 'sami@gulfbank.example' WHERE id = ?",
                  (repo.iso(NOW - timedelta(days=3)), ids["sami"]))
    backup = db.backup_to(tmp_path / "Downloads" / "openberry-backup-2026-10-12.db")

    assert services.send_next_email(now=NOW)["status"] == "sent" and mailbox.sent[-1]["to"] == "lina@gulfbank.example"
    header_id = repo.get_message(ids["lina"]).email_message_id
    repo.record_opt_out(repo.get_message(ids["omar"]).lead_id, "marked in the dashboard")
    with db.connect() as c:  # the connection broke while the server was receiving Rania's email
        c.execute("UPDATE messages SET email_state = 'data', email_claimed_at = ?, email_message_id = ? WHERE id = ?",
                  (repo.iso(NOW + timedelta(minutes=10)), "<rania-1@acme-mail.example>", ids["rania"]))
    done = backups.restore(backup, app_running=lambda: False)
    assert repo.get_message(ids["lina"]).status == "sent"  # kept as sent: it went out
    assert repo.get_message(ids["rania"]).status == "approved"  # as backed up: it may have gone out
    assert repo.get_message(ids["omar"]).status == "draft"  # his opt-out is kept: the approval lapsed

    repo.update_company(company.id, {"outreach": {"email_sending": True}})
    for minutes in (30, 60, 90):
        repo.save_inbox_check(FROM, True, now=NOW + timedelta(minutes=minutes))
        assert services.send_next_email(now=NOW + timedelta(minutes=minutes))["status"] != "sent"
    assert len(mailbox.sent) == 1
    skipped = repo.email_queue(company.id, now=NOW + timedelta(hours=2))["skipped"]
    reasons = {s["message_id"]: s["reason"] for s in skipped}
    assert "step 1 was already emailed to rania@gulfbank.example" in reasons[ids["rania"]]  # may have been sent
    assert repo.suppressed_addresses(["omar@gulfbank.example"])
    assert header_id in repo.inbound_index(now=NOW)["ours"]  # a reply to it is still read
    with db.connect() as c:  # Sami's email, in the backup already, and Lina's, kept as sent, aren't counted twice
        logged = [tuple(r) for r in c.execute("SELECT address, email_state FROM email_sent_log ORDER BY address")]
    assert logged == [("rania@gulfbank.example", "unknown")]
    assert done.kept_email_history


def test_restore_keeps_emails_sent_by_hand_from_data_older_than_the_email_tables(settings, tmp_path):
    """The data being replaced may be older than email sending (0.1.x data restored over before 0.2.0 opened it): an
    email marked sent there still counts."""
    company_id = a_company(leads=0)
    lead, _ = repo.upsert_lead(company_id, LeadIn(full_name="Lina Noor", email="lina@gulfbank.example"))
    msg = repo.create_message(lead.id, "Hello Lina", channel="email", subject="Hi", status="approved")
    backup = db.backup_to(tmp_path / "b.db")
    repo.update_message(msg.id, status="sent")  # sent by hand after the backup
    with closing(sqlite3.connect(settings.db_path)) as c:  # as version 4 had it: no email tables or columns
        for table in ("email_suppressions", "email_events", "email_mailbox", "email_sent_log"):
            c.execute(f"DROP TABLE {table}")
        c.execute("CREATE TABLE messages_v4 AS SELECT id, company_id, lead_id, direction, channel, step, subject, "
                  "body, status, generated_by, created_at, updated_at, sent_at, sent_via, auto_hold, approved_via, "
                  "auto_blocked FROM messages")
        c.execute("DROP TABLE messages")
        c.execute("ALTER TABLE messages_v4 RENAME TO messages")
        c.execute("PRAGMA user_version=4")
        c.commit()
    done = backups.restore(backup, app_running=lambda: False)
    assert repo.get_message(msg.id).status == "sent"
    assert repo.email_queue(company_id)["items"] == [] and done.kept_email_history


@pytest.mark.parametrize("folder_name", ["Backups #2", "A%41"])
def test_restore_from_a_folder_with_a_hash_or_a_percent_in_its_name(settings, tmp_path, folder_name):
    """UPG-5: the path went into a URI unencoded. '#' cut it short (SQLite made an empty file 'Backups ' and checked
    that, read-write), and '%41' was read as 'A'. The data folder too: the copy a restore replaces is read that way."""
    settings.db_path = tmp_path / folder_name / "data" / "openberry.db"
    db.reset_init_cache()
    company_id = a_company()
    backup = db.backup_to(tmp_path / folder_name / "openberry-backup.db")
    repo.add_suppression("ops@z.example", "manual")
    repo.delete_company(company_id)
    assert backups.check_backup_file(backup) == db.SCHEMA_VERSION
    done = backups.restore(backup, app_running=lambda: False)
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"]
    assert done.kept_email_history and repo.suppressed_addresses(["ops@z.example"])
    assert not (tmp_path / "Backups ").exists()


def test_restore_refused_on_the_command_line_exits_with_the_reason(settings, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore", str(tmp_path / "nope.db")])
    assert "There is no file at" in str(exc.value.code) and "Nothing was changed." in str(exc.value.code)


# --------------------------------------------------------------------------------------
# What a restore keeps from the data it replaces, and when it stops (second review, P2-RST-*)
# --------------------------------------------------------------------------------------


def test_the_copy_s_changes_are_in_its_file_before_the_swap(settings, tmp_path):
    """P2-RST-1: the copy's changes stayed in its -wal file when SQLite didn't fold them in as it closed (a full disk:
    it ignores the error there), and the restore deleted that -wal with the swap. It said "Restored" and left a
    malformed file. Each step that changes the copy now folds them into the file itself."""
    company_id = a_company()
    repo.update_company(company_id, {"outreach": {"agent_sending": True}})
    copy = db.backup_to(tmp_path / "copy.db")
    db.init_db(copy, copy_first=False)
    repo.add_suppression("ops@z.example", "manual")
    previous = db.backup_to(tmp_path / "previous.db")
    other = sqlite3.connect(copy)  # SQLite skips that at close while another connection reads the file
    other.execute("SELECT COUNT(*) FROM companies").fetchall()
    wal = copy.with_name(copy.name + "-wal")
    try:
        assert repo.turn_sending_off_in(copy) == ["Acme Chauffeurs"]
        assert not wal.exists() or wal.stat().st_size == 0
        repo.keep_email_history(copy, previous)
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        other.close()
    with closing(sqlite3.connect(copy)) as c:
        assert c.execute("SELECT email FROM email_suppressions").fetchall() == [("ops@z.example",)]


@pytest.mark.parametrize("problem", ["disk_full", "busy"])
def test_a_copy_that_cant_be_saved_whole_stops_the_restore(settings, tmp_path, monkeypatch, problem):
    """P2-RST-1: folding the copy's changes into its file can fail (a full disk) or not finish. The restore stops
    then, before the swap, and says why."""
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    repo.add_suppression("ops@z.example", "manual")
    before = counts(settings.db_path)

    class Copy(sqlite3.Connection):  # the restore's copy of the backup
        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if "wal_checkpoint" in sql:
                if problem == "disk_full":
                    raise sqlite3.OperationalError("database or disk is full")
                return super().execute("SELECT 1, 0, 0")  # busy: it didn't finish
            return super().execute(sql, *args)

    connect = sqlite3.connect

    def connecting(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if ".restore-" in str(path):
            kwargs["factory"] = Copy
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connecting)
    said = {"disk_full": "database or disk is full", "busy": "database is locked"}[problem]
    with pytest.raises(backups.RestoreRefused, match=rf"stopped before your data was replaced \({said}\)"):
        backups.restore(backup, app_running=lambda: False)
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert counts(settings.db_path) == before
    assert not list(settings.db_path.parent.glob(f".{settings.db_path.name}.restore-*"))


def test_a_write_to_the_data_during_the_restore_stops_it(settings, tmp_path, monkeypatch):
    """P2-RST-1: the live data's -wal file is deleted before the swap. A program that wrote after the before-restore
    copy (another OpenBerry) has its changes there: the restore stops rather than lose them."""
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    keep = repo.keep_email_history
    writers: list[sqlite3.Connection] = []

    def and_another_program_writes(path: Path, previous: Path) -> None:
        keep(path, previous)
        writer = sqlite3.connect(settings.db_path)  # still open: its -wal stays
        writers.append(writer)
        writer.execute("INSERT INTO email_suppressions (email, reason, created_at) VALUES ('late@z.example', "
                       "'opt_out', ?)", (repo.iso(),))
        writer.commit()

    monkeypatch.setattr(repo, "keep_email_history", and_another_program_writes)
    try:
        with pytest.raises(backups.RestoreRefused, match="Another program is using your data right now"):
            backups.restore(backup, app_running=lambda: False)
    finally:
        for writer in writers:
            writer.close()
    assert repo.suppressed_addresses(["late@z.example"])


def test_restore_keeps_linkedin_messages_sent_after_the_backup(settings):
    """P2-RST-2: only emails were kept. A LinkedIn message the AI agent sent after the backup was made was approved
    again in it, and the agent was offered it a second time once agent sending was back on."""
    company = repo.create_company(CompanyIn(name="Acme"))
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    ghada, _ = repo.upsert_lead(company.id, LeadIn(full_name="Ghada", linkedin_url="https://www.linkedin.com/in/ghada-x"))
    omar, _ = repo.upsert_lead(company.id, LeadIn(full_name="Omar", linkedin_url="https://www.linkedin.com/in/omar-x"))
    dm = repo.create_message(ghada.id, "Hi Ghada", channel="linkedin_dm", status="approved", generated_by="claude")
    note = repo.create_message(omar.id, "Hi Omar", channel="linkedin_connect", status="approved")
    snap = backups.snapshot(backups.DAILY)
    assert [i["message_id"] for i in repo.send_queue(company.id)["items"]] == [dm.id, note.id]
    repo.confirm_agent_sent(dm.id)  # the agent sent it after the backup was made
    repo.update_message(note.id, status="sent", body="Hi Omar, good to meet you")  # edited, sent by hand, marked sent
    done = backups.restore(snap, app_running=lambda: False)
    kept = repo.get_message(dm.id)
    assert (kept.status, kept.sent_via) == ("sent", "agent") and kept.sent_at is not None
    assert (repo.get_message(note.id).status, repo.get_message(note.id).body) == ("sent", "Hi Omar, good to meet you")
    assert {repo.get_lead(ghada.id).status, repo.get_lead(omar.id).status} == {"contacted"}
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    assert repo.send_queue(company.id)["items"] == [] and done.kept_email_history
    backups.restore(done.previous, app_running=lambda: False)  # back and forth: nothing changes twice
    backups.restore(snap, app_running=lambda: False)
    assert repo.get_message(dm.id).sent_at == kept.sent_at


def test_restore_keeps_opt_outs_of_leads_without_an_email_address(settings):
    """P2-RST-2: Ghada said "don't contact me" on LinkedIn after the backup was made. With no address for the
    never-email list, her opt-out was only on her lead, which the restore put back as it was: her approved LinkedIn
    message went back in the agent's queue. A company that asked by email not to be contacted came off the
    never-contact list the same way."""
    company = repo.create_company(CompanyIn(name="Acme"))
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    ghada, _ = repo.upsert_lead(company.id, LeadIn(full_name="Ghada", linkedin_url="https://www.linkedin.com/in/ghada-x"))
    dm = repo.create_message(ghada.id, "Hi Ghada", channel="linkedin_dm", status="approved", generated_by="claude")
    bank, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Gulf Bank", email="info@gulfbank.example"))
    snap = backups.snapshot(backups.DAILY)
    repo.record_opt_out(ghada.id, "LinkedIn message")
    repo.record_opt_out(bank.id, "phone call")
    backups.restore(snap, app_running=lambda: False)
    lead = repo.get_lead(ghada.id)
    assert "unsubscribed" in lead.tags and lead.status == "disqualified"
    assert f"{repo.OPT_OUT_NOTE} (LinkedIn message)." in lead.notes
    assert repo.get_message(dm.id).status == "draft"  # its approval lapsed, as when she opted out
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    assert repo.send_queue(company.id)["items"] == []
    assert repo.get_company(company.id).icp.exclude_companies == ["Gulf Bank"]
    assert "unsubscribed" in repo.get_lead(bank.id).tags
    backups.restore(snap, app_running=lambda: False)  # again: nothing is added twice
    assert repo.get_lead(ghada.id).notes.count(repo.OPT_OUT_NOTE) == 1
    assert repo.get_lead(ghada.id).tags.count("unsubscribed") == 1
    assert repo.get_company(company.id).icp.exclude_companies == ["Gulf Bank"]


def _reply(sender: str, refers: str, text: str, key: str) -> bytes:
    return (f"From: {sender}\r\nTo: {FROM}\r\nSubject: Re: Hello\r\nMessage-ID: <{key}@mail.example>\r\n"
            f"In-Reply-To: {refers}\r\nReferences: {refers}\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{text}\r\n").encode()


@pytest.mark.parametrize("archived", [True, False])
def test_restore_keeps_replies_recorded_after_the_backup(settings, mailbox, archived):
    """P2-RST-3: Fadi's first email went out before the backup was made, and he replied after it. Once the reply had
    left the inbox (archived), the restored data had no trace of it, and his approved follow-up went out. Hana's
    LinkedIn reply, pasted with log_reply, was lost the same way: her follow-up was due again."""
    company = repo.create_company(CompanyIn(name="Acme Chauffeurs"))
    repo.update_company(company.id, {"outreach": {"email_sending": True, "email_footer": FOOTER,
                                                  "agent_sending": True, "followup_days": [3]}})
    fadi, _ = repo.upsert_lead(company.id, LeadIn(full_name="Fadi", email="fadi@client.example"))
    first = repo.create_message(fadi.id, "Hello Fadi", channel="email", subject="Hello", status="approved",
                                generated_by="claude")
    assert services.send_next_email(now=NOW)["status"] == "sent"
    assert services.check_inbox(now=NOW)["status"] == "ok"  # the inbox is read from here on
    second = repo.create_message(fadi.id, "Following up", channel="email", subject="Hello", step=2,
                                 status="approved", generated_by="claude")
    hana, _ = repo.upsert_lead(company.id, LeadIn(full_name="Hana", linkedin_url="https://www.linkedin.com/in/hana-x"))
    hello = repo.create_message(hana.id, "Hi Hana", channel="linkedin_dm", status="approved", generated_by="claude")
    with db.connect() as c:  # sent a week before
        c.execute("UPDATE messages SET status = 'sent', sent_at = ? WHERE id = ?",
                  (repo.iso(NOW - timedelta(days=7)), hello.id))
    repo.update_lead(hana.id, {"status": "contacted"})
    assert [d["lead"].id for d in repo.followups_due(company.id, now=NOW)] == [hana.id]
    snap = backups.snapshot(backups.DAILY)

    uid = mailbox.deliver(_reply("fadi@client.example", repo.get_message(first.id).email_message_id,
                                 "Thanks, call me next week", "fadi-1"))
    assert services.check_inbox(now=NOW + timedelta(minutes=5))["recorded"] == 1
    repo.log_reply(hana.id, "Not now, thanks")
    if archived:
        del mailbox.folders["INBOX"].messages[uid]
    done = backups.restore(snap, app_running=lambda: False)
    assert done.kept_email_history
    replies = repo.list_messages(company.id, direction="inbound")
    assert sorted(r.lead_id for r in replies) == sorted([fadi.id, hana.id])
    assert repo.get_lead(fadi.id).status == repo.get_lead(hana.id).status == "replied"
    assert repo.get_message(second.id).status == "skipped"  # the reply stopped his sequence, as it did
    repo.update_company(company.id, {"outreach": {"email_sending": True, "agent_sending": True}})
    later = NOW + timedelta(days=1, minutes=30)
    repo.save_inbox_check(FROM, True, now=later)
    assert services.check_inbox(now=later)["status"] == "ok"  # it starts again where the backup was
    assert services.send_next_email(now=later)["status"] != "sent" and len(mailbox.sent) == 1
    assert repo.followups_due(company.id, now=later) == [] and repo.send_queue(company.id, now=later)["items"] == []
    assert len(repo.list_messages(company.id, direction="inbound")) == 2  # the inbox check didn't record it again
    backups.restore(done.previous, app_running=lambda: False)  # back and forth: no reply twice
    backups.restore(snap, app_running=lambda: False)
    assert len(repo.list_messages(company.id, direction="inbound")) == 2


def test_keeping_replies_stays_quick_with_many_messages(settings, tmp_path):
    """P2-RST-3: each reply in the replaced data was looked for among every message of the backup, one at a time (no
    index on lead_id). With 3,000 replies and 30,000 messages that took over 10 seconds, and it grows with both."""
    company = repo.create_company(CompanyIn(name="Acme"))
    now = repo.iso()
    with db.connect() as c:
        c.executemany("INSERT INTO leads (company_id, full_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                      [(company.id, f"Lead {i}", now, now) for i in range(3000)])
        lead_ids = [r[0] for r in c.execute("SELECT id FROM leads ORDER BY id")]
        c.executemany("INSERT INTO messages (company_id, lead_id, channel, body, status, created_at, updated_at) "
                      "VALUES (?, ?, 'linkedin_dm', 'Hi', 'sent', ?, ?)",
                      [(company.id, lead_ids[i % 3000], now, now) for i in range(30000)])
        c.executemany("INSERT INTO messages (company_id, lead_id, direction, channel, body, status, created_at, "
                      "updated_at) VALUES (?, ?, 'inbound', 'linkedin_dm', ?, 'received', ?, ?)",
                      [(company.id, lead_id, f"Thanks {lead_id}", now, now) for lead_id in lead_ids])
    backup = db.backup_to(tmp_path / "b.db")
    repo.log_reply(lead_ids[0], "Call me next week")  # after the backup was made
    started = time.monotonic()
    backups.restore(backup, app_running=lambda: False)
    assert time.monotonic() - started < 5
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM messages WHERE direction = 'inbound'").fetchone()[0] == 3001


@pytest.mark.parametrize("where", ["copying", "reading"])
def test_a_disk_problem_with_the_replaced_data_stops_the_restore(settings, tmp_path, monkeypatch, where):
    """P2-RST-4: with the disk nearly full, copying the data being replaced failed ("database or disk is full"), or
    reading that copy did ("disk I/O error": no room for its -shm file). Both counted as a damaged file: the restore
    went on without the opt-outs and said the data could not be read. Only a damaged file is skipped now."""
    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    repo.add_suppression("ops@z.example", "manual")
    before = counts(settings.db_path)
    if where == "copying":
        snapshot = backups.snapshot

        def no_room(kind: str, **kwargs: Any) -> Path:
            if kind == backups.BEFORE_RESTORE:
                raise sqlite3.OperationalError("database or disk is full")
            return snapshot(kind, **kwargs)

        monkeypatch.setattr(backups, "snapshot", no_room)
    else:
        def no_room(path: Path, previous: Path) -> None:
            exc = sqlite3.OperationalError("disk I/O error")
            exc.sqlite_errorname = "SQLITE_IOERR_SHMSIZE"
            raise exc

        monkeypatch.setattr(repo, "keep_email_history", no_room)
    said = {"copying": "database or disk is full", "reading": "disk I/O error"}[where]
    with pytest.raises(backups.RestoreRefused, match=rf"stopped before your data was replaced \({said}\)"):
        backups.restore(backup, app_running=lambda: False)
    assert counts(settings.db_path) == before and repo.suppressed_addresses(["ops@z.example"])
    assert all(s.path.read_bytes()[:16] == b"SQLite format 3\x00" for s in snaps())  # no damaged file's raw copy


@pytest.mark.parametrize(("path", "uri"), [
    (PureWindowsPath(r"\\NAS\Backups\openberry-backup-2026-10-11.db"),
     "file:////NAS/Backups/openberry-backup-2026-10-11.db?mode=ro"),
    (PureWindowsPath("//NAS/Backups/openberry-backup-2026-10-11.db"),
     "file:////NAS/Backups/openberry-backup-2026-10-11.db?mode=ro"),
    (PureWindowsPath(r"\\NAS\Backups #2\A%41\Dubaï.db"), "file:////NAS/Backups%20%232/A%2541/Duba%C3%AF.db?mode=ro"),
    (PureWindowsPath(r"Z:\Backups\openberry-backup-2026-10-11.db"),
     "file:///Z:/Backups/openberry-backup-2026-10-11.db?mode=ro"),
    (PureWindowsPath(r"C:\Users\Sam\Backups #2\A%41\Dubaï.db"),
     "file:///C:/Users/Sam/Backups%20%232/A%2541/Duba%C3%AF.db?mode=ro"),
    (PurePosixPath("/home/sam/Backups #2/A%41/Dubaï.db"), "file:///home/sam/Backups%20%232/A%2541/Duba%C3%AF.db?mode=ro"),
])
# Python 3.14 deprecates as_uri on a pure path. OpenBerry passes real paths (Path.as_uri): pure ones only test the
# Windows forms on any computer.
@pytest.mark.filterwarnings("ignore:pathlib.PurePath.as_uri:DeprecationWarning")
def test_the_read_only_uri_of_a_backup(path, uri):
    """P2-RST-5: a backup on a network share (\\\\NAS\\Backups, or a mapped drive Windows resolves to one) got the URI
    file://NAS/..., and SQLite refused it: "not an OpenBerry backup (invalid uri authority: NAS)"."""
    assert db.read_only_uri(path) == uri


def test_a_backup_opens_read_only_through_its_uri(settings, tmp_path):
    """P2-RST-5, on this computer's own paths: the URI opens the file itself, and only to read it."""
    a_company()
    backup = db.backup_to(tmp_path / "Backups #2" / "A%41" / "Dubaï" / "openberry-backup.db")
    with closing(sqlite3.connect(db.read_only_uri(backup.absolute()), uri=True)) as c:
        assert c.execute("SELECT name FROM companies").fetchall() == [("Acme Chauffeurs",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("DELETE FROM companies")
    if sys.platform == "win32":  # the same file through this computer's administrative share, if it has one
        unc = Path(rf"\\localhost\{backup.drive[0]}$" + str(backup)[2:])
    else:  # SQLite reads the URI of a network path, file:////server/share/..., as the path //server/share/...
        unc = Path("/" + str(backup))
    if not unc.exists():
        pytest.skip("no administrative share")
    with closing(sqlite3.connect(backup)) as c:  # this checks the path: a WAL file's shared memory is another matter
        c.execute("PRAGMA journal_mode=DELETE")
    assert db.read_only_uri(unc.absolute()).startswith("file:////")
    assert backups.check_backup_file(unc) == db.SCHEMA_VERSION


def test_restore_refuses_while_openberry_serve_runs(settings, tmp_path, monkeypatch):
    """P2-RST-6: only the desktop app was looked for. `openberry serve` sends emails too: one it sent while the
    restore ran was written to the data being replaced, and went out again once sending was back on."""
    import uvicorn

    a_company()
    backup = db.backup_to(tmp_path / "b.db")
    said: list[str] = []

    def serving(app: Any, **options: Any) -> None:
        with pytest.raises(backups.RestoreRefused) as refused:
            backups.restore(backup)
        said.append(str(refused.value))

    monkeypatch.setattr(uvicorn, "run", serving)
    cli.main(["serve"])
    assert said == ["OpenBerry is open. Close OpenBerry (or stop openberry serve) and quit Claude Desktop (right-click "
                    "its icon by the clock, Quit), then try again."]
    assert snaps() == []
    assert not list(config.OPENBERRY_HOME.glob("serve-*.lock"))  # it stopped: its lock went with it
    assert backups.restore(backup).previous is not None


def test_restore_keeps_the_google_maps_monthly_count(settings):
    """P2-RST-7: the count of this month's Google Maps searches went back to the backup's, so up to 900 more could
    run that month, past Google's free 1,000, and be billed."""
    repo.create_company(CompanyIn(name="Acme"))
    for _ in range(10):
        repo.reserve_api_call("google_places_search", 900)
    snap = backups.snapshot(backups.DAILY)
    for _ in range(890):
        repo.reserve_api_call("google_places_search", 900)
    assert not repo.reserve_api_call("google_places_search", 900).granted
    backups.restore(snap, app_running=lambda: False)
    assert repo.api_usage("google_places_search", 900).used == 900
    assert not repo.reserve_api_call("google_places_search", 900).granted


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
    assert "OpenBerry keeps the never-email list and the record of sent emails, and the opt-outs, replies" in text
    assert "check LinkedIn and your mailbox's Sent folder" in text

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

