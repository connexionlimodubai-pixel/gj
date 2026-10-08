"""Data access: companies, leads (with identity merging), signals, messages, scan runs.

Every public function opens its own transaction via `db.connect()` unless a connection
is passed in, so callers in FastAPI, the MCP server and the scheduler stay simple.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import connect
from .models import (
    LEAD_STATUSES,
    MESSAGE_CHANNELS,
    MESSAGE_STATUSES,
    SIGNAL_TYPES,
    Company,
    CompanyIn,
    Lead,
    LeadIn,
    Message,
    ScanRun,
    Signal,
    SignalIn,
)
from .scoring import SignalPoint, score_lead

# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def _conn(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        yield conn
    else:
        with connect() as c:
            yield c


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class NotFound(LookupError):
    pass


# --------------------------------------------------------------------------------------
# Identity keys used to merge the same lead seen via different sources
# --------------------------------------------------------------------------------------

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|gmbh|plc|corp|corporation|co|company|sa|ag|bv|srl|pte|"
    r"pty|fz|fze|fzco|fz-llc|dmcc|holding|holdings|group)\b\.?",
    re.I,
)


def normalize_domain(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    v = v.removeprefix("www.")
    return v if "." in v else ""


def _name_key(name: str) -> str:
    n = _COMPANY_SUFFIXES.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9]+", "", n)
    return f"n:{n}" if n else ""


def _domain_key(domain: str) -> str:
    d = normalize_domain(domain)
    return f"d:{d}" if d else ""


def company_key(name: str, domain: str = "") -> str:
    """The key that links people to their company's account-level intent.

    The normalised name wins because people leads almost always carry a company name but
    rarely a domain; the domain is the fallback for accounts known only by their website.
    """
    return _name_key(name) or _domain_key(domain)


def normalize_linkedin(url: str) -> str:
    m = re.search(r"linkedin\.com/(in|company|pub|school)/([^/?#\s]+)", url or "", re.I)
    return f"{m.group(1).lower()}/{m.group(2).lower()}" if m else ""


# Query parameters that name the profile itself (news.ycombinator.com/user?id=pg) are part of its
# identity; any other query parameters (tracking, ?utm_source=...) and fragments are dropped.
_PROFILE_ID_PARAMS = ("id", "user", "username", "u")


def normalize_profile_url(url: str) -> str:
    base, _, query = re.sub(r"^https?://(www\.)?", "", url.strip().lower()).split("#")[0].partition("?")
    ident = "&".join(p for p in query.split("&")
                     if p.partition("=")[0] in _PROFILE_ID_PARAMS and p.partition("=")[2])
    return base.rstrip("/") + (f"?{ident}" if ident else "")


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def lead_identity_keys(data: LeadIn | Lead, kind: str) -> list[str]:
    keys: list[str] = []
    ckey = company_key(data.lead_company, data.company_domain)
    if kind == "account":
        # Both identities, so "Acme Bank" seen first by name and later with acme.com merge.
        return [f"acct:{k}" for k in (_name_key(data.lead_company), _domain_key(data.company_domain)) if k]
    if li := normalize_linkedin(data.linkedin_url):
        keys.append(f"li:{li}")
    if data.email and "@" in data.email:
        keys.append(f"em:{data.email.strip().lower()}")
    if data.github_username:
        keys.append(f"gh:{data.github_username.strip().lstrip('@').lower()}")
    if data.twitter:
        handle = re.sub(r"^https?://(www\.)?(twitter|x)\.com/", "", data.twitter.strip(), flags=re.I)
        keys.append(f"tw:{handle.lstrip('@').strip('/').lower()}")
    if data.profile_url and not normalize_linkedin(data.profile_url):
        keys.append(f"url:{normalize_profile_url(data.profile_url)}")
    name = _norm_name(data.full_name)
    if name and ckey:
        keys.append(f"nc:{name}|{ckey}")
    elif name and not keys:
        keys.append(f"nm:{name}")
    return keys


# --------------------------------------------------------------------------------------
# Companies
# --------------------------------------------------------------------------------------

_COMPANY_JSON = ("competitors", "best_customers", "icp", "signals", "outreach", "notify")
_COMPANY_COLUMNS = (
    "name", "website", "industry", "location", "company_size", "description", "products",
    "value_proposition", "pain_points", "proof_points", "competitors", "best_customers", "contact_name", "contact_email", "contact_phone",
    "requirements", "leads_per_week", "icp", "signals", "outreach", "notify", "scan_interval_hours", "status",
)


def _company_from_row(row: sqlite3.Row) -> Company:
    data = dict(row)
    for key in _COMPANY_JSON:
        data[key] = _loads(data[key], [] if key in ("competitors", "best_customers") else {})
    return Company.model_validate(data)


def _company_values(data: CompanyIn) -> dict[str, Any]:
    dumped = data.model_dump(mode="json")
    return {
        col: json.dumps(dumped[col]) if col in _COMPANY_JSON else dumped[col]
        for col in _COMPANY_COLUMNS
    }


def create_company(data: CompanyIn, conn: sqlite3.Connection | None = None) -> Company:
    values = _company_values(data)
    now = iso()
    with _conn(conn) as c:
        cols = ", ".join([*values, "created_at", "updated_at"])
        marks = ", ".join(["?"] * (len(values) + 2))
        cur = c.execute(f"INSERT INTO companies ({cols}) VALUES ({marks})", [*values.values(), now, now])
        return get_company(cur.lastrowid, conn=c)


def get_company(company_id: int, conn: sqlite3.Connection | None = None) -> Company:
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise NotFound(f"company {company_id} not found")
    return _company_from_row(row)


def find_company(company_id: int) -> Company | None:
    try:
        return get_company(company_id)
    except NotFound:
        return None


def list_companies(conn: sqlite3.Connection | None = None) -> list[Company]:
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM companies ORDER BY created_at DESC, id DESC").fetchall()
    return [_company_from_row(r) for r in rows]


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "weights":
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def update_company(company_id: int, data: CompanyIn | dict[str, Any],
                   conn: sqlite3.Connection | None = None) -> Company:
    """Replace the profile with a CompanyIn, or deep-merge a partial dict into it."""
    with _conn(conn) as c:
        current = get_company(company_id, conn=c)
        if isinstance(data, dict):
            merged = _deep_merge(current.model_dump(mode="json"), data)
            data = CompanyIn.model_validate(merged)
        values = _company_values(data)
        sets = ", ".join(f"{col} = ?" for col in values)
        c.execute(f"UPDATE companies SET {sets}, updated_at = ? WHERE id = ?",
                  [*values.values(), iso(), company_id])
        rescore_company(company_id, conn=c)
        return get_company(company_id, conn=c)


def delete_company(company_id: int, conn: sqlite3.Connection | None = None) -> None:
    with _conn(conn) as c:
        c.execute("DELETE FROM companies WHERE id = ?", (company_id,))


def set_last_scan(company_id: int, when: datetime | None = None, conn: sqlite3.Connection | None = None) -> None:
    with _conn(conn) as c:
        c.execute("UPDATE companies SET last_scan_at = ? WHERE id = ?", (iso(when), company_id))


def companies_due_for_scan(now: datetime | None = None) -> list[Company]:
    now = now or utcnow()
    due = []
    for company in list_companies():
        if company.status != "active":
            continue
        last = company.last_scan_at
        if last is None or now - last >= timedelta(hours=company.scan_interval_hours):
            due.append(company)
    return due


# --------------------------------------------------------------------------------------
# Leads
# --------------------------------------------------------------------------------------

_LEAD_PROFILE_FIELDS = (
    "full_name", "title", "lead_company", "company_domain", "industry", "company_size", "location",
    "linkedin_url", "email", "phone", "website", "github_username", "twitter", "profile_url", "bio",
)
_LEAD_EDITABLE = (*_LEAD_PROFILE_FIELDS, "status", "notes", "tags", "kind")
LEAD_PROFILE_FIELDS = _LEAD_PROFILE_FIELDS
LEAD_EDITABLE_FIELDS = _LEAD_EDITABLE


def _lead_from_row(row: sqlite3.Row) -> Lead:
    data = dict(row)
    data["score_reasons"] = _loads(data["score_reasons"], [])
    data["tags"] = _loads(data["tags"], [])
    data.pop("company_key", None)
    return Lead.model_validate(data)


def get_lead(lead_id: int, conn: sqlite3.Connection | None = None) -> Lead:
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise NotFound(f"lead {lead_id} not found")
    return _lead_from_row(row)


def find_lead(lead_id: int) -> Lead | None:
    try:
        return get_lead(lead_id)
    except NotFound:
        return None


def _find_existing(c: sqlite3.Connection, company_id: int, keys: list[str]) -> int | None:
    if not keys:
        return None
    marks = ", ".join("?" * len(keys))
    row = c.execute(
        f"SELECT MIN(lead_id) AS id FROM lead_keys WHERE company_id = ? AND key IN ({marks})",
        [company_id, *keys],
    ).fetchone()
    return row["id"] if row and row["id"] is not None else None


def _add_keys(c: sqlite3.Connection, company_id: int, lead_id: int, keys: list[str]) -> None:
    c.executemany(
        "INSERT OR IGNORE INTO lead_keys (company_id, key, lead_id) VALUES (?, ?, ?)",
        [(company_id, k, lead_id) for k in keys],
    )


def upsert_lead(company_id: int, data: LeadIn, conn: sqlite3.Connection | None = None,
                rescore: bool = True) -> tuple[Lead, bool]:
    """Insert a lead or merge it into the existing one with the same identity.

    Merging only fills empty fields, unions tags and appends new notes, so data from a
    richer source (e.g. Claude reading a LinkedIn profile) is never clobbered by a poorer one.
    Signals attached to `data.signals` are recorded against the lead.
    Returns (lead, created).
    """
    kind = "person" if data.full_name.strip() else "account"
    if kind == "account" and not (data.lead_company or data.company_domain):
        raise ValueError("a lead needs at least a full_name or a lead_company")
    keys = lead_identity_keys(data, kind)
    ckey = company_key(data.lead_company, data.company_domain)
    now = iso()
    with _conn(conn) as c:
        get_company(company_id, conn=c)  # raises NotFound
        existing_id = _find_existing(c, company_id, keys)
        if existing_id is None:
            values = {f: getattr(data, f) for f in _LEAD_PROFILE_FIELDS}
            values.update(
                company_id=company_id, kind=kind, company_key=ckey, source=data.source or "manual",
                notes=data.notes, tags=json.dumps(data.tags), created_at=now, updated_at=now,
            )
            cols = ", ".join(values)
            cur = c.execute(f"INSERT INTO leads ({cols}) VALUES ({', '.join('?' * len(values))})",
                            list(values.values()))
            lead_id = cur.lastrowid
            created = True
        else:
            lead_id = existing_id
            created = False
            row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            updates: dict[str, Any] = {}
            for f in _LEAD_PROFILE_FIELDS:
                new = getattr(data, f)
                if new and not row[f]:
                    updates[f] = new
            if data.notes and data.notes not in row["notes"]:
                updates["notes"] = (row["notes"] + "\n" + data.notes).strip()
            tags = _loads(row["tags"], [])
            merged_tags = tags + [t for t in data.tags if t not in tags]
            if merged_tags != tags:
                updates["tags"] = json.dumps(merged_tags)
            if not row["company_key"] and ckey:
                updates["company_key"] = ckey
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                c.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                          [*updates.values(), now, lead_id])
        _add_keys(c, company_id, lead_id, keys)
        for sig in data.signals:
            if not sig.source or sig.source == "manual":
                sig = sig.model_copy(update={"source": data.source or "manual"})
            add_signal(company_id, sig, lead_id=lead_id, conn=c, rescore=False)
        if rescore:
            _rescore_lead_and_dependents(c, lead_id)
        return get_lead(lead_id, conn=c), created


def update_lead(lead_id: int, fields: dict[str, Any], conn: sqlite3.Connection | None = None) -> Lead:
    """Overwrite editable fields (status, notes, tags, profile fields, kind)."""
    unknown = set(fields) - set(_LEAD_EDITABLE)
    if unknown:
        raise ValueError(f"cannot update fields: {', '.join(sorted(unknown))}")
    if "status" in fields and fields["status"] not in LEAD_STATUSES:
        raise ValueError(f"status must be one of {', '.join(LEAD_STATUSES)}")
    if "kind" in fields and fields["kind"] not in ("person", "account"):
        raise ValueError("kind must be 'person' or 'account'")
    with _conn(conn) as c:
        lead = get_lead(lead_id, conn=c)
        updates = dict(fields)
        if "tags" in updates:
            from .models import split_list

            updates["tags"] = json.dumps(split_list(updates["tags"]))
        for key in _LEAD_PROFILE_FIELDS:
            if key in updates:
                updates[key] = str(updates[key] or "").strip()
        if updates:
            merged = lead.model_copy(update={k: v for k, v in fields.items() if k != "tags"})
            if not merged.full_name and not merged.lead_company and not merged.company_domain:
                raise ValueError("a lead needs at least a full_name or a lead_company")
            kind = updates.get("kind", lead.kind)
            if kind == "person" and not merged.full_name:
                kind = "account"
                updates["kind"] = kind
            updates["company_key"] = company_key(merged.lead_company, merged.company_domain)
            sets = ", ".join(f"{k} = ?" for k in updates)
            c.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?", [*updates.values(), iso(), lead_id])
            _add_keys(c, lead.company_id, lead_id, lead_identity_keys(merged, kind))
        _rescore_lead_and_dependents(c, lead_id)
        return get_lead(lead_id, conn=c)


def set_ai_assessment(lead_id: int, ai_score: int | None, rationale: str = "",
                      conn: sqlite3.Connection | None = None) -> Lead:
    if ai_score is not None and not 0 <= ai_score <= 100:
        raise ValueError("ai_score must be between 0 and 100")
    with _conn(conn) as c:
        get_lead(lead_id, conn=c)
        c.execute("UPDATE leads SET ai_score = ?, ai_rationale = ?, updated_at = ? WHERE id = ?",
                  (ai_score, rationale.strip(), iso(), lead_id))
        _rescore_lead(c, lead_id)
        return get_lead(lead_id, conn=c)


def delete_lead(lead_id: int, conn: sqlite3.Connection | None = None) -> None:
    with _conn(conn) as c:
        c.execute("DELETE FROM leads WHERE id = ?", (lead_id,))


_LEAD_SORTS = {
    "score": "score DESC, last_signal_at DESC, id DESC",
    "recent": "created_at DESC, id DESC",
    "signal": "last_signal_at IS NULL, last_signal_at DESC, score DESC",
    "name": "full_name COLLATE NOCASE, lead_company COLLATE NOCASE",
}


def list_leads(company_id: int, *, tier: str | None = None, status: str | None = None,
               min_score: int | None = None, search: str | None = None, kind: str | None = None,
               source: str | None = None, sort: str = "score", limit: int = 50, offset: int = 0,
               conn: sqlite3.Connection | None = None) -> tuple[list[Lead], int]:
    where = ["company_id = ?"]
    params: list[Any] = [company_id]
    if tier:
        where.append("tier = ?")
        params.append(tier)
    if status:
        where.append("status = ?")
        params.append(status)
    if min_score is not None:
        where.append("score >= ?")
        params.append(min_score)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if source:
        where.append("source = ?")
        params.append(source)
    if search:
        like = f"%{search.strip()}%"
        where.append("(full_name LIKE ? OR title LIKE ? OR lead_company LIKE ? OR location LIKE ? "
                     "OR email LIKE ? OR bio LIKE ? OR notes LIKE ?)")
        params.extend([like] * 7)
    clause = " AND ".join(where)
    order = _LEAD_SORTS.get(sort, _LEAD_SORTS["score"])
    limit = max(1, min(int(limit), 5000))
    with _conn(conn) as c:
        total = c.execute(f"SELECT COUNT(*) FROM leads WHERE {clause}", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM leads WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                         [*params, limit, max(0, int(offset))]).fetchall()
    return [_lead_from_row(r) for r in rows], total


def contacts_at_account(lead: Lead, conn: sqlite3.Connection | None = None) -> list[Lead]:
    """People we know at the same company as an account-level lead (or colleagues of a person)."""
    with _conn(conn) as c:
        row = c.execute("SELECT company_key FROM leads WHERE id = ?", (lead.id,)).fetchone()
        if not row or not row["company_key"]:
            return []
        rows = c.execute(
            "SELECT * FROM leads WHERE company_id = ? AND company_key = ? AND kind = 'person' AND id != ? "
            "ORDER BY score DESC LIMIT 20", (lead.company_id, row["company_key"], lead.id)).fetchall()
    return [_lead_from_row(r) for r in rows]


# --------------------------------------------------------------------------------------
# Scoring glue
# --------------------------------------------------------------------------------------


def _signal_points(c: sqlite3.Connection, lead_row: sqlite3.Row) -> list[SignalPoint]:
    rows = c.execute("SELECT type, strength, occurred_at, title FROM signals WHERE lead_id = ?",
                     (lead_row["id"],)).fetchall()
    points = [SignalPoint(r["type"], r["strength"], datetime.fromisoformat(r["occurred_at"]), r["title"])
              for r in rows]
    if lead_row["kind"] == "person" and lead_row["company_key"]:
        acct = c.execute(
            "SELECT s.type, s.strength, s.occurred_at, s.title FROM signals s JOIN leads l ON l.id = s.lead_id "
            "WHERE l.company_id = ? AND l.kind = 'account' AND l.company_key = ?",
            (lead_row["company_id"], lead_row["company_key"])).fetchall()
        points += [SignalPoint(r["type"], r["strength"], datetime.fromisoformat(r["occurred_at"]), r["title"],
                               account_level=True) for r in acct]
    return points


def _rescore_lead(c: sqlite3.Connection, lead_id: int, company: Company | None = None) -> None:
    row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        return
    company = company or get_company(row["company_id"], conn=c)
    points = _signal_points(c, row)
    lead = _lead_from_row(row)
    result = score_lead(lead, company.icp, points, company.signals.weights, lead.ai_score, lead.ai_rationale)
    tier = "cold" if row["status"] == "disqualified" else result.tier
    last = max((p.occurred_at for p in points), default=None)
    c.execute(
        "UPDATE leads SET icp_score = ?, intent_score = ?, score = ?, tier = ?, score_reasons = ?, "
        "last_signal_at = ? WHERE id = ?",
        (result.icp_score, result.intent_score, result.score, tier, json.dumps(result.reasons),
         iso(last) if last else None, lead_id),
    )


def _rescore_lead_and_dependents(c: sqlite3.Connection, lead_id: int) -> None:
    row = c.execute("SELECT company_id, kind, company_key FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        return
    company = get_company(row["company_id"], conn=c)
    _rescore_lead(c, lead_id, company)
    if row["kind"] == "account" and row["company_key"]:
        ids = c.execute("SELECT id FROM leads WHERE company_id = ? AND company_key = ? AND kind = 'person'",
                        (row["company_id"], row["company_key"])).fetchall()
        for r in ids:
            _rescore_lead(c, r["id"], company)


def rescore_company(company_id: int, conn: sqlite3.Connection | None = None) -> int:
    with _conn(conn) as c:
        company = get_company(company_id, conn=c)
        ids = [r["id"] for r in c.execute("SELECT id FROM leads WHERE company_id = ?", (company_id,))]
        for lead_id in ids:
            _rescore_lead(c, lead_id, company)
        return len(ids)


# --------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------


def _signal_from_row(row: sqlite3.Row) -> Signal:
    data = dict(row)
    data.pop("raw", None)
    return Signal.model_validate(data)


def signal_external_id(sig: SignalIn, lead_id: int | None) -> str:
    if sig.external_id:
        return sig.external_id[:200]
    basis = "|".join([sig.type, sig.url, sig.title, str(lead_id or "")])
    return "h:" + hashlib.sha1(basis.encode()).hexdigest()


def add_signal(company_id: int, sig: SignalIn, lead_id: int | None = None,
               conn: sqlite3.Connection | None = None, rescore: bool = True) -> tuple[Signal, bool]:
    """Record a signal. Duplicates (same source + external id) are ignored. Returns (signal, created)."""
    ext = signal_external_id(sig, lead_id)
    occurred = iso(sig.occurred_at) if sig.occurred_at else iso()
    with _conn(conn) as c:
        if lead_id is not None:
            owner = c.execute("SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if owner is None or owner["company_id"] != company_id:
                raise NotFound(f"lead {lead_id} not found in company {company_id}")
        cur = c.execute(
            "INSERT OR IGNORE INTO signals (company_id, lead_id, type, source, external_id, title, summary, url, "
            "strength, occurred_at, created_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, lead_id, sig.type if sig.type in SIGNAL_TYPES else "custom", sig.source or "manual", ext,
             sig.title[:500], sig.summary[:4000], sig.url[:1000], sig.strength, occurred, iso(),
             json.dumps(sig.raw, default=str)[:20000]),
        )
        created = cur.rowcount == 1
        row = c.execute("SELECT * FROM signals WHERE company_id = ? AND source = ? AND external_id = ?",
                        (company_id, sig.source or "manual", ext)).fetchone()
        if created and rescore and lead_id is not None:
            _rescore_lead_and_dependents(c, lead_id)
        return _signal_from_row(row), created


def list_signals(company_id: int, *, lead_id: int | None = None, type: str | None = None,
                 source: str | None = None, since: datetime | None = None, limit: int = 100,
                 offset: int = 0, include_account: bool = False,
                 conn: sqlite3.Connection | None = None) -> tuple[list[Signal], int]:
    where = ["s.company_id = ?"]
    params: list[Any] = [company_id]
    if lead_id is not None:
        if include_account:
            where.append("(s.lead_id = ? OR s.lead_id IN (SELECT a.id FROM leads a JOIN leads p "
                         "ON a.company_id = p.company_id AND a.company_key = p.company_key "
                         "WHERE p.id = ? AND a.kind = 'account' AND p.kind = 'person' AND p.company_key != ''))")
            params.extend([lead_id, lead_id])
        else:
            where.append("s.lead_id = ?")
            params.append(lead_id)
    if type:
        where.append("s.type = ?")
        params.append(type)
    if source:
        where.append("s.source = ?")
        params.append(source)
    if since:
        where.append("s.occurred_at >= ?")
        params.append(iso(since))
    clause = " AND ".join(where)
    with _conn(conn) as c:
        total = c.execute(f"SELECT COUNT(*) FROM signals s WHERE {clause}", params).fetchone()[0]
        rows = c.execute(f"SELECT s.* FROM signals s WHERE {clause} ORDER BY s.occurred_at DESC, s.id DESC "
                         "LIMIT ? OFFSET ?", [*params, max(1, min(int(limit), 1000)), max(0, int(offset))]).fetchall()
    return [_signal_from_row(r) for r in rows], total


# --------------------------------------------------------------------------------------
# Outreach messages
# --------------------------------------------------------------------------------------


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message.model_validate(dict(row))


def create_message(lead_id: int, body: str, *, channel: str = "linkedin_dm", subject: str = "", step: int = 1,
                   generated_by: str = "template", status: str = "draft", direction: str = "outbound",
                   conn: sqlite3.Connection | None = None) -> Message:
    if channel not in MESSAGE_CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(MESSAGE_CHANNELS)}")
    if status not in MESSAGE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MESSAGE_STATUSES)}")
    if direction not in ("outbound", "inbound"):
        raise ValueError("direction must be 'outbound' or 'inbound'")
    if not body.strip():
        raise ValueError("message body is empty")
    now = iso()
    with _conn(conn) as c:
        lead = get_lead(lead_id, conn=c)
        cur = c.execute(
            "INSERT INTO messages (company_id, lead_id, direction, channel, step, subject, body, status, generated_by, "
            "created_at, updated_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lead.company_id, lead_id, direction, channel, max(1, int(step)), subject.strip(), body.strip(), status,
             generated_by, now, now, now if status in ("sent", "received") else None),
        )
        return get_message(cur.lastrowid, conn=c)


def log_reply(lead_id: int, body: str, *, channel: str = "linkedin_dm",
              conn: sqlite3.Connection | None = None) -> Message:
    """Record a reply the lead sent us (pasted from LinkedIn/email). Moves the lead to 'replied'."""
    with _conn(conn) as c:
        msg = create_message(lead_id, body, channel=channel, status="received", direction="inbound",
                             generated_by="lead", conn=c)
        lead = get_lead(lead_id, conn=c)
        order = list(LEAD_STATUSES)
        if lead.status in ("new", "qualified", "contacted") or order.index(lead.status) < order.index("replied"):
            c.execute("UPDATE leads SET status = 'replied', updated_at = ? WHERE id = ?", (iso(), lead_id))
        # Stop the sequence: pending drafts for this lead are no longer relevant.
        c.execute("UPDATE messages SET status = 'skipped', updated_at = ? WHERE lead_id = ? AND direction = 'outbound' "
                  "AND status IN ('draft', 'approved')", (iso(), lead_id))
        return msg


def followups_due(company_id: int, now: datetime | None = None,
                  conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Leads whose last outbound message was sent long enough ago that the next follow-up is due.

    Uses the company's outreach.followup_days / max_followups. Leads that replied, or that
    already have an unsent draft queued, are skipped.
    """
    now = now or utcnow()
    with _conn(conn) as c:
        company = get_company(company_id, conn=c)
        days = company.outreach.followup_days or [3]
        rows = c.execute(
            "SELECT m.lead_id, MAX(m.step) AS step, MAX(m.sent_at) AS last_sent FROM messages m "
            "JOIN leads l ON l.id = m.lead_id WHERE m.company_id = ? AND m.direction = 'outbound' "
            "AND m.status = 'sent' AND l.status IN ('contacted') GROUP BY m.lead_id", (company_id,)).fetchall()
        due = []
        for r in rows:
            step = r["step"]
            if step > company.outreach.max_followups or not r["last_sent"]:
                continue
            pending = c.execute("SELECT COUNT(*) FROM messages WHERE lead_id = ? AND direction = 'outbound' "
                                "AND status IN ('draft', 'approved')", (r["lead_id"],)).fetchone()[0]
            inbound = c.execute("SELECT COUNT(*) FROM messages WHERE lead_id = ? AND direction = 'inbound'",
                                (r["lead_id"],)).fetchone()[0]
            if pending or inbound:
                continue
            wait = days[min(step - 1, len(days) - 1)]
            due_at = datetime.fromisoformat(r["last_sent"]) + timedelta(days=wait)
            if due_at <= now:
                due.append({"lead": get_lead(r["lead_id"], conn=c), "next_step": step + 1, "due_since": due_at})
        due.sort(key=lambda d: d["due_since"])
        return due


def get_message(message_id: int, conn: sqlite3.Connection | None = None) -> Message:
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise NotFound(f"message {message_id} not found")
    return _message_from_row(row)


def update_message(message_id: int, *, status: str | None = None, body: str | None = None,
                   subject: str | None = None, conn: sqlite3.Connection | None = None) -> Message:
    """Edit a message. Marking it sent/replied also advances the lead's pipeline status."""
    with _conn(conn) as c:
        msg = get_message(message_id, conn=c)
        updates: dict[str, Any] = {}
        if body is not None:
            if not body.strip():
                raise ValueError("message body is empty")
            updates["body"] = body.strip()
        if subject is not None:
            updates["subject"] = subject.strip()
        if status is not None:
            if status not in MESSAGE_STATUSES:
                raise ValueError(f"status must be one of {', '.join(MESSAGE_STATUSES)}")
            updates["status"] = status
            if status == "sent" and not msg.sent_at:
                updates["sent_at"] = iso()
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            c.execute(f"UPDATE messages SET {sets}, updated_at = ? WHERE id = ?", [*updates.values(), iso(), message_id])
        if status in ("sent", "replied"):
            lead = get_lead(msg.lead_id, conn=c)
            order = list(LEAD_STATUSES)
            target = "contacted" if status == "sent" else "replied"
            if lead.status in ("new", "qualified") or (
                    status == "replied" and order.index(lead.status) < order.index("replied")):
                c.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (target, iso(), lead.id))
        return get_message(message_id, conn=c)


def delete_message(message_id: int, conn: sqlite3.Connection | None = None) -> None:
    with _conn(conn) as c:
        c.execute("DELETE FROM messages WHERE id = ?", (message_id,))


def list_messages(company_id: int, *, status: str | None = None, lead_id: int | None = None,
                  direction: str | None = None, limit: int = 100, offset: int = 0,
                  conn: sqlite3.Connection | None = None) -> list[Message]:
    where = ["company_id = ?"]
    params: list[Any] = [company_id]
    if direction:
        where.append("direction = ?")
        params.append(direction)
    if status:
        where.append("status = ?")
        params.append(status)
    if lead_id is not None:
        where.append("lead_id = ?")
        params.append(lead_id)
    with _conn(conn) as c:
        rows = c.execute(f"SELECT * FROM messages WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC "
                         "LIMIT ? OFFSET ?", [*params, max(1, min(int(limit), 1000)), max(0, int(offset))]).fetchall()
    return [_message_from_row(r) for r in rows]


# --------------------------------------------------------------------------------------
# Scan runs
# --------------------------------------------------------------------------------------


def _scan_from_row(row: sqlite3.Row) -> ScanRun:
    data = dict(row)
    data["stats"] = _loads(data["stats"], {})
    return ScanRun.model_validate(data)


def start_scan_run(company_id: int, trigger: str = "manual", conn: sqlite3.Connection | None = None) -> int:
    with _conn(conn) as c:
        cur = c.execute("INSERT INTO scan_runs (company_id, trigger, status, started_at) VALUES (?, ?, 'running', ?)",
                        (company_id, trigger, iso()))
        return cur.lastrowid


def finish_scan_run(run_id: int, status: str, stats: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    with _conn(conn) as c:
        c.execute("UPDATE scan_runs SET status = ?, finished_at = ?, stats = ? WHERE id = ?",
                  (status, iso(), json.dumps(stats, default=str), run_id))


def list_scan_runs(company_id: int, limit: int = 10, conn: sqlite3.Connection | None = None) -> list[ScanRun]:
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM scan_runs WHERE company_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
                         (company_id, limit)).fetchall()
    return [_scan_from_row(r) for r in rows]


def get_scan_run(run_id: int, conn: sqlite3.Connection | None = None) -> ScanRun:
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM scan_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFound(f"scan run {run_id} not found")
    return _scan_from_row(row)


# --------------------------------------------------------------------------------------
# Dashboard numbers
# --------------------------------------------------------------------------------------


def company_stats(company_id: int, now: datetime | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    now = now or utcnow()
    with _conn(conn) as c:
        def count(sql: str, *params: Any) -> int:
            return c.execute(sql, params).fetchone()[0]

        tiers = {t: 0 for t in ("hot", "warm", "cold")}
        for r in c.execute("SELECT tier, COUNT(*) n FROM leads WHERE company_id = ? AND kind = 'person' GROUP BY tier",
                           (company_id,)):
            tiers[r["tier"]] = r["n"]
        statuses = {s: 0 for s in LEAD_STATUSES}
        for r in c.execute("SELECT status, COUNT(*) n FROM leads WHERE company_id = ? GROUP BY status", (company_id,)):
            statuses[r["status"]] = r["n"]
        messages = {s: 0 for s in MESSAGE_STATUSES}
        for r in c.execute("SELECT status, COUNT(*) n FROM messages WHERE company_id = ? AND direction = 'outbound' "
                           "GROUP BY status", (company_id,)):
            messages[r["status"]] = r["n"]

        start = (now - timedelta(days=13)).date()
        by_day = {(start + timedelta(days=i)).isoformat(): 0 for i in range(14)}
        for r in c.execute("SELECT substr(occurred_at, 1, 10) d, COUNT(*) n FROM signals WHERE company_id = ? "
                           "AND occurred_at >= ? GROUP BY d", (company_id, start.isoformat())):
            if r["d"] in by_day:
                by_day[r["d"]] = r["n"]
        by_type = [
            {"type": r["type"], "label": SIGNAL_TYPES.get(r["type"], SIGNAL_TYPES["custom"])[0], "count": r["n"]}
            for r in c.execute("SELECT type, COUNT(*) n FROM signals WHERE company_id = ? AND occurred_at >= ? "
                               "GROUP BY type ORDER BY n DESC", (company_id, iso(now - timedelta(days=30))))
        ]
        by_source = [
            {"source": r["source"], "count": r["n"]}
            for r in c.execute("SELECT source, COUNT(*) n FROM signals WHERE company_id = ? AND occurred_at >= ? "
                               "GROUP BY source ORDER BY n DESC", (company_id, iso(now - timedelta(days=30))))
        ]
        reached = sum(statuses[s] for s in ("contacted", "replied", "meeting", "won", "lost"))
        answered = sum(statuses[s] for s in ("replied", "meeting", "won"))
        runs = list_scan_runs(company_id, limit=1, conn=c)
        return {
            "leads_total": count("SELECT COUNT(*) FROM leads WHERE company_id = ?", company_id),
            "people": count("SELECT COUNT(*) FROM leads WHERE company_id = ? AND kind = 'person'", company_id),
            "accounts": count("SELECT COUNT(*) FROM leads WHERE company_id = ? AND kind = 'account'", company_id),
            "new_leads_7d": count("SELECT COUNT(*) FROM leads WHERE company_id = ? AND created_at >= ?",
                                  company_id, iso(now - timedelta(days=7))),
            "tiers": tiers,
            "statuses": statuses,
            "signals_total": count("SELECT COUNT(*) FROM signals WHERE company_id = ?", company_id),
            "signals_7d": count("SELECT COUNT(*) FROM signals WHERE company_id = ? AND occurred_at >= ?",
                                company_id, iso(now - timedelta(days=7))),
            "signals_by_day": [{"date": d, "count": n} for d, n in by_day.items()],
            "signals_by_type": by_type,
            "signals_by_source": by_source,
            "messages": messages,
            "reply_rate": round(100 * answered / reached) if reached else None,
            "last_scan": runs[0] if runs else None,
        }
