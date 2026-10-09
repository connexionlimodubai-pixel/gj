"""SQLite storage. One file, WAL mode, so the dashboard and the MCP server can share it."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings

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
    sent_via TEXT NOT NULL DEFAULT ''  -- who recorded the send: '' the user, 'agent' the AI agent, 'claude' Claude
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
"""

SCHEMA_VERSION = 3


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database written by an older version up to SCHEMA (CREATE IF NOT EXISTS adds no columns)."""
    conn.execute("BEGIN IMMEDIATE")  # two processes starting at once must not both add the column
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
        if "alerted_at" not in columns:
            conn.execute("ALTER TABLE leads ADD COLUMN alerted_at TEXT")
            # Leads already hot were alerted by the scan that made them hot: don't alert them all again.
            conn.execute("UPDATE leads SET alerted_at = updated_at WHERE tier = 'hot'")
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        if "sent_via" not in message_columns:  # version 3: AI agent sending counts the agent's sends
            conn.execute("ALTER TABLE messages ADD COLUMN sent_via TEXT NOT NULL DEFAULT ''")
        # After the column exists (an old database gets it just above): the agent's rolling 24-hour count.
        conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_company_sent ON messages(company_id, sent_via, sent_at)")
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


def init_db(path: Path | None = None) -> Path:
    path = path or db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)  # another process may be upgrading the same file
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()
    return path


_initialized: set[str] = set()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error.

    A fresh connection per unit of work keeps us thread-safe across FastAPI's
    threadpool, the scheduler task, and the MCP server process.
    """
    path = db_path()
    key = str(path.resolve()) if str(path) != ":memory:" else ":memory:"
    if key not in _initialized:
        init_db(path)
        _initialized.add(key)
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
    """Forget which DB files were initialised (tests switch DB paths)."""
    _initialized.clear()
