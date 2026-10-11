"""SQLite storage. One file, WAL mode, so the dashboard and the MCP server can share it."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings, make_private, private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    website TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    company_size TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    products TEXT NOT NULL DEFAULT '',
    value_proposition TEXT NOT NULL DEFAULT '',
    pain_points TEXT NOT NULL DEFAULT '',
    proof_points TEXT NOT NULL DEFAULT '',
    competitors TEXT NOT NULL DEFAULT '[]',
    best_customers TEXT NOT NULL DEFAULT '[]',
    contact_name TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '',
    leads_per_week INTEGER NOT NULL DEFAULT 50,
    icp TEXT NOT NULL DEFAULT '{}',
    signals TEXT NOT NULL DEFAULT '{}',
    outreach TEXT NOT NULL DEFAULT '{}',
    notify TEXT NOT NULL DEFAULT '{}',
    scan_interval_hours INTEGER NOT NULL DEFAULT 24,
    status TEXT NOT NULL DEFAULT 'active',
    last_scan_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'person',
    full_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    lead_company TEXT NOT NULL DEFAULT '',
    company_domain TEXT NOT NULL DEFAULT '',
    company_key TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    company_size TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    linkedin_url TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    github_username TEXT NOT NULL DEFAULT '',
    twitter TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    icp_score INTEGER NOT NULL DEFAULT 0,
    intent_score INTEGER NOT NULL DEFAULT 0,
    ai_score INTEGER,
    ai_rationale TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT 'cold',
    score_reasons TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    last_signal_at TEXT,
    alerted_at TEXT,  -- when the hot-lead alert / auto-draft handled this lead; NULL again once it goes cold
    alert_failed_at TEXT,  -- (version 6) when its alert first failed to reach any webhook: tried again for 24 hours
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_leads_company_score ON leads(company_id, score DESC);
CREATE INDEX IF NOT EXISTS ix_leads_company_key ON leads(company_id, company_key);

-- Every identity we know for a lead (LinkedIn URL, email, GitHub login, name+company...).
-- Used to merge the same person seen through different sources.
CREATE TABLE IF NOT EXISTS lead_keys (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    PRIMARY KEY (company_id, key)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    strength INTEGER NOT NULL DEFAULT 50,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw TEXT NOT NULL DEFAULT '{}',
    UNIQUE (company_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS ix_signals_company_time ON signals(company_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_signals_lead ON signals(lead_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direction TEXT NOT NULL DEFAULT 'outbound',
    channel TEXT NOT NULL DEFAULT 'linkedin_dm',
    step INTEGER NOT NULL DEFAULT 1,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    generated_by TEXT NOT NULL DEFAULT 'template',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    sent_via TEXT NOT NULL DEFAULT '',  -- who recorded the send: '' the user, 'agent' the AI agent, 'claude' Claude
    auto_hold INTEGER NOT NULL DEFAULT 0,  -- 1: the user held this draft, so auto-approve never approves it
    approved_via TEXT NOT NULL DEFAULT '',  -- 'auto': auto-approve approved it after its review window
    auto_blocked INTEGER NOT NULL DEFAULT 0,  -- 1: auto-approve found a reason not to approve this draft (yet)
    -- Email sending (version 5), outbound emails only: '' | claimed | data | sent | failed | unknown | bounced
    email_state TEXT NOT NULL DEFAULT '',
    email_message_id TEXT,  -- the Message-ID header OpenBerry gave it, at its first claim; never changed
    email_to TEXT NOT NULL DEFAULT '',  -- the address it was sent to (the lead's email may change later)
    email_claim TEXT NOT NULL DEFAULT '',  -- the sending worker's random token while it is claimed
    email_claimed_at TEXT,
    email_attempts INTEGER NOT NULL DEFAULT 0,
    send_error TEXT NOT NULL DEFAULT '',  -- why sending failed, in plain words
    email_from TEXT NOT NULL DEFAULT '',  -- (version 6) a reply read in the inbox: who wrote it (maybe a colleague)
    opt_out_hint TEXT NOT NULL DEFAULT ''  -- (version 6) a reply's words that look like an opt-out, for the user
);
CREATE INDEX IF NOT EXISTS ix_messages_company_status ON messages(company_id, status);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_scan_runs_company ON scan_runs(company_id, started_at DESC);

-- Paid API calls, per service and calendar month (UTC): the monthly cap on Google Maps searches. One row per
-- month; repo.reserve_api_call adds to it under BEGIN IMMEDIATE so processes never go over the cap together.
CREATE TABLE IF NOT EXISTS api_usage (
    service TEXT NOT NULL,              -- e.g. 'google_places_search'
    month TEXT NOT NULL,                -- 'YYYY-MM', UTC
    used INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (service, month)
);

-- Google Maps searches (signals.places_queries): when each company last read every page of each search, so a
-- search runs at most once a week. Only the search text and our own counts: nothing from Google.
CREATE TABLE IF NOT EXISTS place_searches (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    query_key TEXT NOT NULL,            -- the search, casefolded, single spaces
    searched_at TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,   -- businesses Google listed
    added INTEGER NOT NULL DEFAULT 0,   -- leads they gave
    PRIMARY KEY (company_id, query_key)
);

-- Google Place IDs a company already handled. Google allows storing Place IDs indefinitely and nothing else
-- from Places. added = 1: it became a lead and is never added again, even after that lead is deleted;
-- added = 0: skipped (no usable website, robots.txt, excluded), checked again 30 days after checked_at.
CREATE TABLE IF NOT EXISTS place_ids (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    place_id TEXT NOT NULL,
    added INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (company_id, place_id)
);

-- Email sending (schema version 5). Addresses OpenBerry never emails again: an opt-out, a hard bounce, or the user's
-- own choice. For every company (one mailbox per install), and they outlive the lead, so a business found again on
-- Google Maps stays suppressed.
CREATE TABLE IF NOT EXISTS email_suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,                 -- lower case, trimmed
    reason TEXT NOT NULL,                -- 'opt_out' | 'bounce' | 'manual'
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    detail TEXT NOT NULL DEFAULT '',     -- the bounce's status and the server's words, or the opt-out's words (short)
    created_at TEXT NOT NULL,
    UNIQUE (email, reason)
);

-- What the email sender and the inbox check did: replies, opt-outs, bounces, auto-replies, failures. inbound_key (the
-- inbox message's Message-ID, else its folder and UID) makes sure an inbox message is handled once.
CREATE TABLE IF NOT EXISTS email_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,  -- kept when the company is deleted (version 6)
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    inbound_key TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_email_events_inbound ON email_events(inbound_key) WHERE inbound_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_email_events_company ON email_events(company_id, created_at);

-- The mailbox's own state, one row per account (the username it signs in with: changing the From address to an alias
-- keeps it, changing mailboxes starts fresh): its pause, failures in a row, the inbox check (when it last worked, its
-- last error, and the last UID read in each folder).
CREATE TABLE IF NOT EXISTS email_mailbox (
    address TEXT PRIMARY KEY,                 -- the account: the sign-in username, lower case (Settings.mailbox_key)
    paused_until TEXT,
    pause_reason TEXT NOT NULL DEFAULT '',    -- why auth_failed is set
    auth_failed INTEGER NOT NULL DEFAULT 0,   -- 1: a login refused (no inbox check either), 2: a settings problem;
                                              -- nothing is sent until the settings are saved or Try again
    failures INTEGER NOT NULL DEFAULT 0,      -- temporary sending failures in a row
    imap_state TEXT NOT NULL DEFAULT '{}',    -- {"INBOX": {"uidvalidity": n, "last_uid": n}, ...}
    imap_checked_at TEXT,                     -- the last inbox check that worked
    imap_error TEXT NOT NULL DEFAULT '',      -- why the last inbox check failed ('' when it worked)
    imap_tried_at TEXT,                       -- the last inbox check, whatever happened
    updated_at TEXT NOT NULL,
    until_reason TEXT NOT NULL DEFAULT ''     -- (version 6) why paused_until is set (a provider's block, failures)
);

-- Version 6. Emails that went out or may have (and attempts), copied here when their message, lead or company is
-- deleted: "never the same step twice to an address", the follow-up wait, the daily limits and the minutes between
-- two emails keep counting them, and a reply, opt-out or bounce that refers to one is still read. No cascade.
CREATE TABLE IF NOT EXISTS email_sent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,                    -- lower case: where it went
    step INTEGER NOT NULL DEFAULT 1,
    email_message_id TEXT,                    -- its Message-ID header (none: sent by hand)
    email_state TEXT NOT NULL DEFAULT '',     -- as messages.email_state: sent | unknown | bounced | failed | ''
    sent_via TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    claimed_at TEXT,
    replied_at TEXT,                          -- the address had replied (any reply through that lead)
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_email_sent_log_address ON email_sent_log(address);
"""

SCHEMA_VERSION = 6
# Columns schema version 5 added to messages: outbound emails OpenBerry sends from the user's mailbox.
EMAIL_MESSAGE_COLUMNS = {
    "email_state": "TEXT NOT NULL DEFAULT ''",
    "email_message_id": "TEXT",
    "email_to": "TEXT NOT NULL DEFAULT ''",
    "email_claim": "TEXT NOT NULL DEFAULT ''",
    "email_claimed_at": "TEXT",
    "email_attempts": "INTEGER NOT NULL DEFAULT 0",
    "send_error": "TEXT NOT NULL DEFAULT ''",
    "email_from": "TEXT NOT NULL DEFAULT ''",  # version 6
    "opt_out_hint": "TEXT NOT NULL DEFAULT ''",  # version 6
}


def _keep_email_events(conn: sqlite3.Connection) -> None:
    """Version 6: email_events outlive a deleted company (company_id becomes NULL): SQLite can't change a foreign key,
    so the table is rebuilt once, with its rows and indexes."""
    links = {row[3]: row[6] for row in conn.execute("PRAGMA foreign_key_list(email_events)")}  # column -> on_delete
    if links.get("company_id", "").upper() != "CASCADE":
        return
    conn.execute("""CREATE TABLE email_events_v6 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
        lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
        message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        inbound_key TEXT,
        created_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO email_events_v6 (id, company_id, lead_id, message_id, kind, detail, inbound_key, "
                 "created_at) SELECT id, company_id, lead_id, message_id, kind, detail, inbound_key, created_at "
                 "FROM email_events")
    conn.execute("DROP TABLE email_events")
    conn.execute("ALTER TABLE email_events_v6 RENAME TO email_events")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_email_events_inbound ON email_events(inbound_key) "
                 "WHERE inbound_key IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_email_events_company ON email_events(company_id, created_at)")


log = logging.getLogger(__name__)


class DatabaseProblem(RuntimeError):
    """The data file can't be opened as it is. The message says why and what to do, in plain words: the desktop app
    shows it in a window, the dashboard on its error page, Claude as a tool error, and the command line prints it."""


class DatabaseTooNew(DatabaseProblem):
    """The file was written by a newer OpenBerry: this version would damage it (0.1.1 turned a v5 file into v3)."""

    def __init__(self, version: int) -> None:
        self.version = version
        super().__init__(f"This data was saved by a newer OpenBerry (database v{version}). Open the newer app, "
                         "or restore a backup.")


class UpgradeBackupFailed(DatabaseProblem):
    """The copy taken before a schema upgrade failed (a full disk, a folder that can't be written): nothing changed."""

    def __init__(self, folder: Path, reason: object) -> None:
        super().__init__(f"OpenBerry has to update your data for this version, but it could not save a copy of it "
                         f"first in {folder} ({reason}). Nothing was changed. Free some disk space, or check that you "
                         "can write to that folder, then open OpenBerry again.")


# --------------------------------------------------------------------------------------
# Database errors in plain words (the dashboard's error page, the desktop app, the logs)
# --------------------------------------------------------------------------------------

DISK_FULL = "Your disk is full, so OpenBerry can't save anything. Free some space, then try again."
DAMAGED = "OpenBerry's data file is damaged: restore a backup (Your data)."
BUSY = "The database is busy, try again in a moment."
DAMAGED_WORDS = ("malformed", "not a database", "disk i/o error")


def log_location() -> str:
    """Where this process's log goes: the desktop app's log file, else the terminal or service that runs it."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            return handler.baseFilename
    return "the window or terminal where OpenBerry runs"


def is_disk_full(exc: BaseException | None) -> bool:
    text = str(exc).lower()
    return (isinstance(exc, OSError) and exc.errno == errno.ENOSPC) or (
        isinstance(exc, (OSError, sqlite3.Error)) and ("disk is full" in text or "no space left" in text))


def problem_text(exc: BaseException, path: Path | None = None) -> str | None:
    """A database error in plain words, naming the data folder and the log file; None for any other error."""
    text = str(exc).lower()
    code = str(getattr(exc, "sqlite_errorname", "") or "")
    if is_disk_full(exc) or code.startswith("SQLITE_FULL"):
        base = DISK_FULL
    elif isinstance(exc, sqlite3.DatabaseError) and (code.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB", "SQLITE_IOERR"))
                                                     or any(w in text for w in DAMAGED_WORDS)):
        base = DAMAGED
    elif isinstance(exc, sqlite3.OperationalError) and (code.startswith(("SQLITE_BUSY", "SQLITE_LOCKED"))
                                                        or "locked" in text or "busy" in text):
        base = BUSY
    else:
        return None
    return f"{base} {_where(path)}"


def _where(path: Path | None = None) -> str:
    return f"Your data is in {Path(path or db_path()).resolve().parent}. Details are in {log_location()}."


def damaged_text(path: Path | None = None) -> str:
    return f"{DAMAGED} {_where(path)}"


_reported: set[str] = set()


def report_once(logger: logging.Logger, what: str, exc: BaseException) -> None:
    """Log a database problem (a full disk, a damaged or too new file) once, with its traceback, rather than on every
    scheduler tick or request until it is fixed. Repeats go to the debug level."""
    key = f"{type(exc).__name__}: {exc}"
    if key in _reported:
        logger.debug("%s: %s (again)", what, key)
        return
    _reported.add(key)
    logger.error("%s: %s", what, exc, exc_info=exc)


def forget_reported() -> None:
    """Things work again: log the next problem, even one seen before."""
    _reported.clear()


_checked: dict[str, str] = {}


def quick_check(path: Path | None = None) -> str:
    """PRAGMA quick_check, once per process and file: "" when the file is sound (or doesn't exist yet), else what
    SQLite found wrong."""
    path = Path(path or db_path())
    if str(path) == ":memory:" or not path.is_file():
        return ""
    key = str(path.resolve())
    if key not in _checked:
        try:
            with contextlib.closing(sqlite3.connect(path, timeout=30)) as conn:
                rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check(5)")]
            _checked[key] = "" if rows == ["ok"] else "; ".join(rows)
        except sqlite3.DatabaseError as exc:
            _checked[key] = str(exc)
    return _checked[key]


def backup_to(dest: Path, source: Path | None = None) -> Path:
    """Copy the database (`source`, else the configured one) to `dest` with SQLite's backup API.

    The copy is consistent and complete: it holds what is still in the -wal file, and other connections keep reading
    and writing while it is made. It is written under a temporary name first, so `dest` is either a whole copy or
    absent. Returns `dest`.
    """
    source = Path(source or db_path())
    dest = Path(dest)
    if not source.is_file():
        raise FileNotFoundError(f"there is no database at {source}")
    private_dir(dest.parent)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        with contextlib.closing(sqlite3.connect(source, timeout=30)) as src, \
                contextlib.closing(sqlite3.connect(tmp)) as out:
            src.execute("PRAGMA busy_timeout=30000")
            src.backup(out)
        make_private(tmp)  # a copy of the same personal data
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return dest


def file_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _before_upgrade(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse a file a newer version wrote, and copy a file an older version wrote before anything in it changes.

    Runs before executescript(SCHEMA), which creates tables outside the migration's transaction, and writes nothing.
    """
    version = file_version(conn)
    if version > SCHEMA_VERSION:
        raise DatabaseTooNew(version)
    if 0 < version < SCHEMA_VERSION and str(path) != ":memory:":
        from . import backups  # it imports this module

        try:
            copy = backups.snapshot(backups.PRE_UPGRADE, source=path, version=version)
        except (OSError, sqlite3.Error) as exc:
            raise UpgradeBackupFailed(backups.folder(path), exc) from exc
        log.info("upgrading the data from database v%s to v%s; a copy of it was saved first: %s", version,
                 SCHEMA_VERSION, copy)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database written by an older version up to SCHEMA (CREATE IF NOT EXISTS adds no columns)."""
    conn.execute("BEGIN IMMEDIATE")  # two processes starting at once must not both add the column
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:  # a newer version upgraded it since _before_upgrade looked
            raise DatabaseTooNew(version)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
        if "alerted_at" not in columns:
            conn.execute("ALTER TABLE leads ADD COLUMN alerted_at TEXT")
            # Leads already hot were alerted by the scan that made them hot: don't alert them all again.
            conn.execute("UPDATE leads SET alerted_at = updated_at WHERE tier = 'hot'")
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        if "sent_via" not in message_columns:  # version 3: AI agent sending counts the agent's sends
            conn.execute("ALTER TABLE messages ADD COLUMN sent_via TEXT NOT NULL DEFAULT ''")
        if "auto_hold" not in message_columns:  # version 4: auto-approve (drafts the user held stay drafts)
            conn.execute("ALTER TABLE messages ADD COLUMN auto_hold INTEGER NOT NULL DEFAULT 0")
        if "approved_via" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN approved_via TEXT NOT NULL DEFAULT ''")
        if "auto_blocked" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN auto_blocked INTEGER NOT NULL DEFAULT 0")
        for column, kind in EMAIL_MESSAGE_COLUMNS.items():  # version 5: email sending; 6: who wrote, opt-out hints
            if column not in message_columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {kind}")
        if "alert_failed_at" not in columns:  # version 6: hot-lead alerts that failed are tried again
            conn.execute("ALTER TABLE leads ADD COLUMN alert_failed_at TEXT")
        if "until_reason" not in {row[1] for row in conn.execute("PRAGMA table_info(email_mailbox)")}:
            # Version 6: a provider's block keeps its own reason when a refused login is fixed.
            conn.execute("ALTER TABLE email_mailbox ADD COLUMN until_reason TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE email_mailbox SET until_reason = pause_reason WHERE paused_until IS NOT NULL "
                         "AND auth_failed = 0")
        _keep_email_events(conn)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_email_mid ON messages(email_message_id) "
                     "WHERE email_message_id IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_email_state ON messages(email_state) "
                     "WHERE email_state != ''")
        # After the column exists (an old database gets it just above): the agent's rolling 24-hour count.
        conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_company_sent ON messages(company_id, sent_via, sent_at)")
        # Older versions left sent_at empty on an outbound message recorded straight away as 'replied' (sent, then
        # answered). The connection-request limits count sends by sent_at, so date those by their last update.
        conn.execute("UPDATE messages SET sent_at = updated_at WHERE direction = 'outbound' "
                     "AND status IN ('sent', 'replied') AND sent_at IS NULL")
        if 0 < version < 2:  # identity keys became Unicode-aware (accents, Arabic, CJK...): re-key old rows
            from . import repo

            conn.row_factory = sqlite3.Row
            repo.refresh_identity_keys(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def db_path() -> Path:
    return get_settings().db_path


def _use_wal(conn: sqlite3.Connection, timeout: float = 30.0) -> None:
    """Switch the file to WAL (once: later calls see it already is). Changing the journal mode needs the file to itself
    for a moment and SQLite doesn't wait for that: while another connection writes or reads a new file it fails at once
    with "database is locked", so retry for as long as the busy timeout would wait."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def init_db(path: Path | None = None) -> Path:
    path = path or db_path()
    if str(path) != ":memory:":
        private_dir(path.parent)
    conn = sqlite3.connect(path, timeout=30)  # another process may be upgrading the same file
    try:
        _use_wal(conn)
        _before_upgrade(conn, path)
        conn.executescript(SCHEMA)
        _migrate(conn)
        if str(path) != ":memory:":  # lead personal data and inbox replies: for this user only
            make_private(path, *(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")))
    finally:
        conn.close()
    return path


_initialized: set[str] = set()
_init_lock = threading.Lock()  # one thread per process creates and upgrades the file; the others wait for it


def prepare(path: Path | None = None) -> Path:
    """Create or upgrade the database once per process (init_db), before its first connection. Raises a
    DatabaseProblem when the file can't be used; it is tried again on the next connection."""
    path = path or db_path()
    key = str(path.resolve()) if str(path) != ":memory:" else ":memory:"
    if key not in _initialized:
        with _init_lock:
            if key not in _initialized:
                init_db(path)
                _initialized.add(key)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error.

    A fresh connection per unit of work keeps us thread-safe across FastAPI's
    threadpool, the scheduler task, and the MCP server process.
    """
    path = prepare()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_init_cache() -> None:
    """Forget which DB files were initialised and checked (tests switch DB paths; a restore swaps the file)."""
    _initialized.clear()
    _checked.clear()
