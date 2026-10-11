"""Backups of the database (one SQLite file holds everything: companies, leads, messages, email opt-outs).

- Snapshots: copies OpenBerry saves by itself in the backups folder next to the database (`backups/`): every day
  (the dashboard's scheduler), before a schema upgrade (db.init_db), before Delete company and before a restore. Each
  kind keeps its newest few (KEEP); daily ones keep 7 days and 4 weeks.
- `openberry backup [PATH]` and the Your data page's Download backup: a copy whenever the user wants one. Copies the
  user asked for are never deleted.
- `openberry restore FILE`: puts a backup back, while OpenBerry is closed.

Every copy goes through db.backup_to (SQLite's backup API), so it is consistent even while OpenBerry runs, and holds
what is still in the -wal file. Copying openberry.db alone with the file manager would miss that.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import private_dir

log = logging.getLogger(__name__)

FOLDER = "backups"
STAMP = "%Y%m%d-%H%M%S"
MANUAL = "manual"            # openberry backup: openberry-YYYYMMDD-HHMMSS.db, never deleted by OpenBerry
DAILY = "daily"              # the scheduler, once a day
BEFORE_DELETE = "before-delete"    # Delete company, and removing the sample company
BEFORE_RESTORE = "before-restore"  # the data a restore replaced
PRE_UPGRADE = "pre-upgrade"  # db.init_db, before a schema upgrade: openberry-pre-vN-YYYYMMDD-HHMMSS.db
# How many of each kind are kept (the newest). Daily snapshots keep DAILY_DAYS days and DAILY_WEEKS weeks instead.
KEEP = {BEFORE_DELETE: 10, BEFORE_RESTORE: 5, PRE_UPGRADE: 3}
DAILY_DAYS = 7
DAILY_WEEKS = 4
DAILY_EVERY = timedelta(hours=23)  # the scheduler ticks every few minutes: "a day" without drifting later each day
CLOCK_SLACK = timedelta(minutes=5)  # a daily snapshot dated later than this (the clock went back) doesn't count
RESTORE_LOCK_TIMEOUT = 2.0  # seconds a restore waits for another program to finish writing
LABELS = {
    MANUAL: "Backup you made", DAILY: "Daily", BEFORE_DELETE: "Before a company was deleted",
    BEFORE_RESTORE: "Before a restore", PRE_UPGRADE: "Before an update",
}
NAME = re.compile(r"openberry-(?:pre-v(?P<version>\d{1,4})-)?(?P<stamp>\d{8}-\d{6})(?:-(?P<n>\d{1,3}))?"
                  r"(?:-(?P<kind>daily|before-delete|before-restore))?\.db")


def folder(db_file: Path | None = None) -> Path:
    """The backups folder: next to the database, in the data folder (OPENBERRY_HOME, or Docker's /data volume)."""
    return Path(db_file or db.db_path()).parent / FOLDER


def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def snapshot_name(kind: str, now: datetime | None = None, version: int | None = None, n: int = 1) -> str:
    stamp = _utc(now).strftime(STAMP) + (f"-{n}" if n > 1 else "")
    if kind == PRE_UPGRADE:
        return f"openberry-pre-v{version}-{stamp}.db"
    return f"openberry-{stamp}.db" if kind == MANUAL else f"openberry-{stamp}-{kind}.db"


@dataclass(frozen=True)
class Snapshot:
    path: Path
    kind: str
    created: datetime  # UTC, from the file name
    version: int | None = None  # the database version a pre-upgrade copy holds
    n: int = 1  # 2, 3... for a second copy of the same kind in the same second

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def label(self) -> str:
        if self.kind == PRE_UPGRADE:
            return f"{LABELS[PRE_UPGRADE]} (database v{self.version})"
        return LABELS[self.kind]

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def parse(path: Path) -> Snapshot | None:
    """The snapshot a backups-folder file is, or None for any other file (OpenBerry never touches those)."""
    match = NAME.fullmatch(path.name)
    if match is None:
        return None
    try:
        created = datetime.strptime(match["stamp"], STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    n = int(match["n"] or 1)
    if match["version"]:
        return Snapshot(path, PRE_UPGRADE, created, int(match["version"]), n)
    return Snapshot(path, match["kind"] or MANUAL, created, n=n)


def list_snapshots(db_file: Path | None = None) -> list[Snapshot]:
    """The backups folder's copies, newest first."""
    try:
        paths = [p for p in folder(db_file).iterdir() if p.is_file()]
    except OSError:  # no folder yet
        return []
    found = [s for s in map(parse, paths) if s is not None]
    return sorted(found, key=lambda s: (s.created, s.n), reverse=True)


def find(name: str, db_file: Path | None = None) -> Snapshot | None:
    """The snapshot with this exact file name (the Your data page's download links), else None."""
    return next((s for s in list_snapshots(db_file) if s.name == name), None)


def snapshot(kind: str, *, source: Path | None = None, now: datetime | None = None, version: int | None = None,
             prune_after: bool = True) -> Path:
    """Copy the database (db.backup_to) into the backups folder as a `kind` snapshot, then delete that kind's oldest
    copies beyond what it keeps. Returns the new file. Raises (OSError, sqlite3.Error) when the copy fails: callers
    about to delete or change data stop then."""
    source = Path(source or db.db_path())
    target = private_dir(folder(source))
    n = 1
    while (dest := target / snapshot_name(kind, now, version, n)).exists():
        n += 1
    db.backup_to(dest, source)
    if prune_after:
        prune(kind, source, now=now)
    return dest


def daily_to_delete(snapshots: list[Snapshot], days: int = DAILY_DAYS, weeks: int = DAILY_WEEKS,
                    now: datetime | None = None) -> list[Snapshot]:
    """Daily snapshots past what is kept: the newest of each of the last `days` days, and the newest of each of the
    `weeks` weeks before those (ISO weeks). Ones dated after `now` (the clock went back since) are left alone: they
    take no day's place, and are sorted out once their date has passed."""
    if now is not None:
        snapshots = [s for s in snapshots if s.created <= _utc(now) + CLOCK_SLACK]
    newest_first = sorted(snapshots, key=lambda s: (s.created, s.n), reverse=True)
    keep: list[Snapshot] = []
    kept_days: list[date] = []
    for s in newest_first:
        if s.created.date() not in kept_days and len(kept_days) < days:
            kept_days.append(s.created.date())
            keep.append(s)
    kept_weeks: list[tuple[int, int]] = []
    oldest_day = min(kept_days, default=None)
    for s in newest_first:
        if oldest_day is not None and s.created.date() >= oldest_day:
            continue
        week = tuple(s.created.isocalendar())[:2]
        if week not in kept_weeks and len(kept_weeks) < weeks:
            kept_weeks.append(week)  # type: ignore[arg-type]
            keep.append(s)
    return [s for s in snapshots if s not in keep]


def prune(kind: str, db_file: Path | None = None, now: datetime | None = None) -> list[Path]:
    """Delete the copies of `kind` beyond what it keeps (copies the user made are never deleted). Returns them."""
    if kind == MANUAL:
        return []
    mine = [s for s in list_snapshots(db_file) if s.kind == kind]
    doomed = daily_to_delete(mine, now=_utc(now)) if kind == DAILY else mine[KEEP.get(kind, len(mine)):]
    deleted = []
    for s in doomed:
        try:
            s.path.unlink()
            deleted.append(s.path)
        except OSError as exc:  # Windows: open in another program; tried again next time
            log.warning("could not delete the old backup %s: %s", s.path, exc)
    return deleted


def daily_due(now: datetime | None = None, db_file: Path | None = None) -> bool:
    """No daily snapshot in the last DAILY_EVERY. One dated in the future (the clock went back) doesn't count."""
    now = _utc(now)
    return not any(now - DAILY_EVERY < s.created <= now + CLOCK_SLACK
                   for s in list_snapshots(db_file) if s.kind == DAILY)


def daily_snapshot(now: datetime | None = None) -> Path | None:
    """The scheduler's daily copy: a snapshot when one is due, else None. Keeps 7 days and 4 weeks of them."""
    source = db.db_path()
    if str(source) == ":memory:" or not source.is_file() or not daily_due(now, source):
        return None
    db.prepare(source)  # an older file is upgraded first (with its own copy): the daily copy is of the data in use
    path = snapshot(DAILY, source=source, now=now)
    log.info("daily backup saved: %s", path)
    return path


class BackupRefused(RuntimeError):
    """`openberry backup` saved nothing. The message says why, in plain words."""


def manual_backup(dest: Path | None = None, now: datetime | None = None, overwrite: bool = False) -> Path:
    """`openberry backup [PATH]`: a copy at PATH, else in the backups folder. Never deleted by OpenBerry.

    A folder, or a name without an extension that doesn't exist yet (made then, private), gets a new copy inside it
    with the usual name. Any other name is the copy's file name: a file already there is replaced only with
    `overwrite`, else BackupRefused."""
    source = db.db_path()
    if dest is None:
        return snapshot(MANUAL, source=source, now=now, prune_after=False)
    dest = Path(dest).expanduser()
    if dest.is_dir() or (not dest.exists() and not dest.suffix):
        target = dest
        n = 1
        while (dest := target / snapshot_name(MANUAL, now, n=n)).exists():
            n += 1
    elif dest.exists() and not overwrite:
        raise BackupRefused(f"There is already a file at {dest}. Give a folder to save a new copy in it each time, or "
                            "a new file name. To replace that file, add --overwrite.")
    return db.backup_to(dest, source)  # makes a missing folder, private


def delete_company(company_id: int) -> Path:
    """Delete company, after a before-delete snapshot taken under the write lock: the copy holds exactly what is
    deleted. A backup that fails raises (OSError, sqlite3.Error) and nothing is deleted. Returns the copy."""
    from . import repo

    with repo.write_locked() as c:
        repo.get_company(company_id, conn=c)  # NotFound before anything is copied
        path = snapshot(BEFORE_DELETE)
        repo.delete_company(company_id, conn=c)
    return path


def download_name(now: datetime | None = None) -> str:
    """The file name the Your data page's Download backup gives the browser."""
    return f"openberry-backup-{_utc(now):%Y-%m-%d}.db"


# --------------------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------------------

class RestoreRefused(RuntimeError):
    """The restore didn't start, or stopped before the backup took the live file's place: nothing changed. The message
    says why, in plain words."""


@dataclass(frozen=True)
class Restored:
    previous: Path | None  # the snapshot of the data the restore replaced (None: there was none)
    version: int  # the backup's database version (it was upgraded when older)
    sending_turned_off: list[str]  # companies whose email or AI agent sending was on in the backup
    kept_email_history: bool = False  # what the replaced data knows of opt-outs, replies and sends was kept


def check_backup_file(path: Path) -> int:
    """Check that `path` is a whole OpenBerry database this version can open. Returns its database version.
    RestoreRefused says what is wrong."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise RestoreRefused(f"There is no file at {path}.")
    try:
        # absolute, not resolve: Windows would turn a mapped drive (Z:) into its \\server\share path.
        with contextlib.closing(sqlite3.connect(db.read_only_uri(path.absolute()), uri=True)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = db.file_version(conn)
    except sqlite3.Error as exc:
        raise RestoreRefused(f"{path} is not an OpenBerry backup ({exc}).") from exc
    if result != "ok":
        raise RestoreRefused(f"{path} is damaged (SQLite's check says: {result}). Choose another backup.")
    if not {"companies", "leads", "messages"} <= tables:
        raise RestoreRefused(f"{path} is not an OpenBerry backup: it has no companies, leads or messages.")
    if version > db.SCHEMA_VERSION:
        raise RestoreRefused(f"{path} was saved by a newer OpenBerry (database v{version}). Restore it with that "
                             "version, or choose an older backup.")
    return version


def _app_running() -> bool:
    """The desktop app or `openberry serve` runs on this data: both send emails, and write while the restore runs."""
    from .config import OPENBERRY_HOME
    from .desktop import app_is_running, serve_is_running

    return app_is_running(OPENBERRY_HOME) or serve_is_running(OPENBERRY_HOME)


CLOSE_FIRST = ("Close OpenBerry (or stop openberry serve) and quit Claude Desktop (right-click its icon by the clock, "
               "Quit), then try again.")


def _busy(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


def _raw_copy(live: Path, now: datetime | None) -> Path:
    """Keep a damaged file (and its -wal) as it is: SQLite can't read it to make a proper copy."""
    target = private_dir(folder(live))
    n = 1
    while (dest := target / snapshot_name(BEFORE_RESTORE, now, n=n)).exists():
        n += 1
    shutil.copy2(live, dest)
    wal = live.with_name(live.name + "-wal")
    if wal.exists():
        shutil.copy2(wal, dest.with_name(dest.name + "-wal"))
    prune(BEFORE_RESTORE, live, now=now)
    return dest


def _set_aside(live: Path, now: datetime | None) -> tuple[Path, bool]:
    """Snapshot the live data while holding its write lock, then fold its -wal file into it (a checkpoint), so
    deleting the -wal loses nothing. RestoreRefused while another program writes or reads it. A damaged file is kept
    as it is (that is often why a backup is restored). Any other error, a full disk say, stops the restore. Returns
    the copy, and whether it is a damaged file's."""
    busy = RestoreRefused("Another program is using your data right now. " + CLOSE_FIRST)
    conn = sqlite3.connect(live, timeout=RESTORE_LOCK_TIMEOUT, isolation_level=None)
    try:
        try:
            conn.execute("BEGIN EXCLUSIVE")
            try:
                previous = snapshot(BEFORE_RESTORE, source=live, now=now)
            finally:
                conn.execute("ROLLBACK")
            blocked = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            if _busy(exc):
                raise busy from exc
            if not db.is_damaged(exc):
                raise
            log.warning("the data being replaced is damaged (%s): keeping its files as they are", exc)
            return _raw_copy(live, now), True
        if blocked:
            raise busy
    finally:
        conn.close()
    return previous, False


def _unsaved(path: Path) -> bool:
    """The database file at `path` has changes in its -wal file: deleting that would lose them (a checkpoint empties
    it)."""
    wal = path.with_name(path.name + "-wal")
    return wal.exists() and wal.stat().st_size > 0


def _keep_email_history(incoming: Path, previous: Path, damaged: bool) -> bool:
    """repo.keep_email_history from `previous`, read-only. A damaged file's copy that can't be read is skipped, with a
    warning, and the restore goes on: returns False. That is also a file that opens but has a damaged table (SQLite
    says it is malformed). Any other error stops the restore: a full disk, or a disk I/O error (one can't make the
    -shm file). Reading `previous` makes -wal and -shm files next to it: those are deleted after."""
    from . import repo

    made = [p for p in (previous.with_name(previous.name + s) for s in ("-wal", "-shm")) if not p.exists()]
    try:
        repo.keep_email_history(incoming, previous)
    except sqlite3.Error as exc:
        if not (damaged or db.is_damaged(exc)):
            raise
        log.warning("could not read the damaged data being replaced (%s): its opt-outs, never-email list, replies and "
                    "record of sent messages are not kept", exc)
        return False
    finally:
        for path in made:
            with contextlib.suppress(OSError):
                path.unlink()
    return True


def restore(backup: Path, *, app_running: Callable[[], bool] | None = None, now: datetime | None = None) -> Restored:
    """Put `backup` back as the live database (`openberry restore FILE`).

    Refuses while the desktop app or `openberry serve` runs, or another program is writing the database. The backup is
    checked first (integrity_check, a version this OpenBerry can open), then copied next to the live file, and the copy
    is made ready: upgraded when older, email sending and AI agent sending turned off for every company (messages sent
    after the backup was made aren't in it, so its approved ones would go out a second time), and what the replaced
    data knows of opt-outs, replies and sends copied in (repo.keep_email_history). The live data is saved as a
    before-restore snapshot, its -wal and -shm files are deleted (they belong to the replaced data and would be
    replayed into the backup), and the copy takes its place (os.replace).

    That swap is the last step: stopped before it, the live data is as it was (anything that fails raises
    RestoreRefused); after it, nothing is left to do.
    """
    from . import repo  # repo imports db, which this module's callers import first

    backup = Path(backup).expanduser()
    live = Path(db.db_path())
    if str(live) == ":memory:":
        raise RestoreRefused("There is no data file to restore into.")
    try:
        if backup.exists() and live.exists() and os.path.samefile(backup, live):
            raise RestoreRefused(f"{backup} is the data file OpenBerry uses now: choose a backup.")
        version = check_backup_file(backup)
        if (app_running or _app_running)():
            raise RestoreRefused("OpenBerry is open. " + CLOSE_FIRST)
        private_dir(live.parent)
    except OSError as exc:  # the backup's folder can't be read (a network drive that is offline), say
        raise RestoreRefused(f"The restore stopped before your data was replaced ({exc}).") from exc
    incoming = live.with_name(f".{live.name}.restore-{os.getpid()}.tmp")
    previous: Path | None = None
    kept = False
    try:
        db.backup_to(incoming, backup)  # the user's file stays where it is, untouched
        db.init_db(incoming, copy_first=False)  # an older backup is upgraded in the copy: the backup is its copy
        turned_off = repo.turn_sending_off_in(incoming)
        if live.exists():
            previous, damaged = _set_aside(live, now)
            kept = _keep_email_history(incoming, previous, damaged)
            if not damaged and _unsaved(live):  # written since its snapshot: deleting its -wal would lose that
                raise RestoreRefused("Another program is using your data right now. " + CLOSE_FIRST)
        if _unsaved(incoming):  # repo._file_write_locked saved the copy's changes in its file: deleted after the swap
            raise sqlite3.OperationalError("the copy of the backup is not completely saved")
        for suffix in ("-wal", "-shm", "-journal"):
            leftover = live.with_name(live.name + suffix)
            if leftover.exists():
                leftover.unlink()
        os.replace(incoming, live)  # the commit point: the backup is in place, ready, with sending off
    except PermissionError as exc:  # Windows: another program has the file open
        raise RestoreRefused("Another program is using your data right now. " + CLOSE_FIRST) from exc
    except (OSError, sqlite3.Error, db.DatabaseProblem) as exc:  # a full disk, say
        raise RestoreRefused(f"The restore stopped before your data was replaced ({exc}).") from exc
    finally:
        for path in (incoming, *(incoming.with_name(incoming.name + s) for s in ("-wal", "-shm"))):
            with contextlib.suppress(OSError):
                path.unlink()
    db.reset_init_cache()  # the next connection opens the restored file afresh
    return Restored(previous, version, turned_off, kept)
