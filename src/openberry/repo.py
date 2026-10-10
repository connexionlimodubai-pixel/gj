"""Data access: companies, leads (with identity merging), signals, messages, scan runs.

Every public function opens its own transaction via `db.connect()` unless a connection
is passed in, so callers in FastAPI, the MCP server and the scheduler stay simple.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .db import connect
from .models import (
    AGENT_CHANNELS,
    AGENT_PAUSE_REASON_MAX,
    APPROVED_VIA_AUTO,
    AUTO_APPROVE_SETTINGS,
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
from .outreach import (
    LINKEDIN_CONNECT_LIMIT,
    LINKEDIN_CONNECT_LIMIT_FREE,
    account_label,
    connect_note_limit,
    monthly_note_limit,
    unfilled_placeholder,
)
from .scoring import DISQUALIFIED_MAX_SCORE, LeadFacts, SignalPoint, icp_fit, score_lead

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


@contextmanager
def _write_locked(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """A connection that holds SQLite's write lock from the first read (BEGIN IMMEDIATE), so the checks and the
    writes that follow them see no other writer in between. A caller's own connection is used as it is."""
    if conn is not None:
        yield conn
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
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


def _fold(text: str) -> str:
    """Casefold and drop accents and vowel points (Société -> societe) but keep the letters of every script.

    Symbols go first so compatibility forms don't turn "Acme™" into "acmetm"; NFKD still folds
    full-width letters and ligatures.
    """
    s = "".join(" " if unicodedata.category(ch)[0] == "S" else ch for ch in text or "")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()


def _words(text: str) -> list[str]:
    """Runs of letters, digits and marks in any script (\\w alone misses Devanagari or Thai vowel signs)."""
    return "".join(ch if ch.isalnum() or unicodedata.category(ch)[0] == "M" else " " for ch in text).split()


def _name_key(name: str) -> str:
    n = "".join(_words(_COMPANY_SUFFIXES.sub(" ", _fold(name))))
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
    return " ".join(_words(_fold(name)))


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
    """Replace the profile with a CompanyIn, or deep-merge a partial dict into it.

    The AI agent's pause (outreach.agent_paused_until / agent_pause_reason) is never changed here: it is
    kept as stored. report_send_problem sets it and resume_agent_sending lifts it. The stored pause is read
    under the write lock, so a save racing a problem report (e.g. a profile form read before the report)
    can't lift the pause.
    A whole profile (a CompanyIn: the dashboard's profile form, which doesn't show them) also keeps the
    auto-approve settings as stored, read under the same lock, so a profile save never turns auto-approve
    back on after the user turned it off. They change through a partial dict (the Outreach page's card).
    Activating a paused company with auto-approve on works like turning auto-approve on (auto_approve_since =
    now): the drafts that waited while it was paused get a full review window, never approved all at once.
    """
    with _write_locked(conn) as c:
        current = get_company(company_id, conn=c)
        kept = {"agent_paused_until": current.outreach.agent_paused_until,
                "agent_pause_reason": current.outreach.agent_pause_reason}
        if isinstance(data, dict):
            merged = _deep_merge(current.model_dump(mode="json"), data)
            data = CompanyIn.model_validate(merged)
        else:
            kept.update({key: getattr(current.outreach, key) for key in AUTO_APPROVE_SETTINGS})
        if current.status != "active" and data.status == "active" and kept.get("auto_approve",
                                                                              data.outreach.auto_approve):
            kept["auto_approve_since"] = utcnow()
        data = data.model_copy(update={"outreach": data.outreach.model_copy(update=kept)})
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


RETRY_FAILED_SCAN_AFTER = timedelta(hours=1)


def companies_due_for_scan(now: datetime | None = None) -> list[Company]:
    """Active companies whose interval has passed (an hour after a failed scan), unless a scan is running."""
    now = now or utcnow()
    due = []
    with connect() as c:
        for company in list_companies(conn=c):
            if company.status != "active" or running_scan_run(company.id, now, conn=c):
                continue
            interval = timedelta(hours=company.scan_interval_hours)
            last_run = next(iter(list_scan_runs(company.id, limit=1, conn=c)), None)
            if last_run is not None and last_run.status == "failed":
                interval = min(interval, RETRY_FAILED_SCAN_AFTER)
            last = company.last_scan_at
            if last is None or now - last >= interval:
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


_WEAK_KEY_PREFIXES = ("nc:", "nm:")  # name (+ company): shared by namesakes, so never enough on its own


def _identity_kind(key: str) -> str:
    """'li', 'em', 'gh', 'tw' or 'url:<host>' (profiles on two different sites don't conflict)."""
    prefix, _, rest = key.partition(":")
    return f"url:{re.split(r'[/?]', rest, maxsplit=1)[0]}" if prefix == "url" else prefix


def _lead_with_key(c: sqlite3.Connection, company_id: int, keys: list[str]) -> int | None:
    if not keys:
        return None
    marks = ", ".join("?" * len(keys))
    row = c.execute(
        f"SELECT MIN(lead_id) AS id FROM lead_keys WHERE company_id = ? AND key IN ({marks})",
        [company_id, *keys],
    ).fetchone()
    return row["id"] if row and row["id"] is not None else None


def _find_existing(c: sqlite3.Connection, company_id: int, keys: list[str]) -> int | None:
    """The lead these identity keys belong to, if any.

    Strong keys (LinkedIn, email, GitHub, X, profile URL, account name/domain) win. A name match
    alone is trusted only when the two records don't carry different identities of the same kind:
    two John Smiths at Google with different LinkedIn profiles are two people.
    """
    strong = [k for k in keys if not k.startswith(_WEAK_KEY_PREFIXES)]
    if (found := _lead_with_key(c, company_id, strong)) is not None:
        return found
    found = _lead_with_key(c, company_id, [k for k in keys if k.startswith(_WEAK_KEY_PREFIXES)])
    if found is None or not strong:
        return found
    # None of our strong keys is known, so any key of the same kind on `found` is a different one.
    theirs = {_identity_kind(r["key"]) for r in c.execute(
        "SELECT key FROM lead_keys WHERE company_id = ? AND lead_id = ?", (company_id, found))}
    return None if theirs & {_identity_kind(k) for k in strong} else found


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
            # The name beats the domain, so a domain-only account that learns its name moves to the
            # key its people already have.
            new_ckey = company_key(updates.get("lead_company", row["lead_company"]),
                                   updates.get("company_domain", row["company_domain"]))
            if new_ckey and new_ckey != row["company_key"]:
                updates["company_key"] = new_ckey
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                c.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                          [*updates.values(), now, lead_id])
                keys = keys + lead_identity_keys(get_lead(lead_id, conn=c), row["kind"])
                if linkedin_profile_url(updates.get("linkedin_url", "")):
                    _unapprove_for_new_profile(c, lead_id)  # the lead had no LinkedIn profile when approved
            if row["kind"] == "account" and row["company_key"] and "company_key" in updates:
                _rescore_people_at(c, company_id, row["company_key"])  # they no longer inherit its intent
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
        old_ckey = c.execute("SELECT company_key FROM leads WHERE id = ?", (lead_id,)).fetchone()["company_key"]
        updates = dict(fields)
        if "tags" in updates:
            from .models import split_list

            updates["tags"] = json.dumps(split_list(updates["tags"]))
        if "notes" in updates:  # NOT NULL column: null clears the notes
            updates["notes"] = str(updates["notes"] or "")
        for key in _LEAD_PROFILE_FIELDS:
            if key in updates:
                updates[key] = str(updates[key] or "").strip()
        if updates:
            merged = lead.model_copy(update={k: v for k, v in updates.items() if k != "tags"})
            if not merged.full_name and not merged.lead_company and not merged.company_domain:
                raise ValueError("a lead needs at least a full_name or a lead_company")
            kind = updates.get("kind", lead.kind)
            if "kind" not in updates and updates.get("full_name"):
                kind = "person"  # like upsert_lead: naming an account's contact makes it a person
            if kind == "person" and not merged.full_name:
                kind = "account"
            updates["kind"] = kind
            new_ckey = updates["company_key"] = company_key(merged.lead_company, merged.company_domain)
            sets = ", ".join(f"{k} = ?" for k in updates)
            c.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?", [*updates.values(), iso(), lead_id])
            if (linkedin_profile_url(merged.linkedin_url) != linkedin_profile_url(lead.linkedin_url)
                    or (merged.status in NO_AGENT_LEAD_STATUSES and lead.status not in NO_AGENT_LEAD_STATUSES)):
                # A new recipient, or a lead that left the pipeline (replied, won, lost, disqualified...): its
                # LinkedIn approvals lapse and its LinkedIn drafts are held, so setting the status back later doesn't
                # put old messages in the agent's queue (nor let auto-approve approve them at once).
                _unapprove_for_new_profile(c, lead_id)
            if kind != lead.kind:
                # Keys of the old kind would keep routing its signals here (company-level ones to a person).
                op = "NOT LIKE" if kind == "account" else "LIKE"
                c.execute(f"DELETE FROM lead_keys WHERE lead_id = ? AND key {op} 'acct:%'", (lead_id,))
            _add_keys(c, lead.company_id, lead_id, lead_identity_keys(merged, kind))
            if lead.kind == "account" and old_ckey and (kind != "account" or new_ckey != old_ckey):
                _rescore_people_at(c, lead.company_id, old_ckey)  # they no longer inherit its intent
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
        row = c.execute("SELECT company_id, kind, company_key FROM leads WHERE id = ?", (lead_id,)).fetchone()
        c.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        if row is not None and row["kind"] == "account" and row["company_key"]:
            _rescore_people_at(c, row["company_id"], row["company_key"])  # its signals are gone with it


def _rescore_people_at(c: sqlite3.Connection, company_id: int, ckey: str) -> None:
    """Rescore the people under a company key, e.g. after its account lead was deleted or moved."""
    company = get_company(company_id, conn=c)
    rows = c.execute("SELECT id FROM leads WHERE company_id = ? AND company_key = ? AND kind = 'person'",
                     (company_id, ckey)).fetchall()
    for r in rows:
        _rescore_lead(c, r["id"], company)


def refresh_identity_keys(conn: sqlite3.Connection | None = None) -> int:
    """Recompute every lead's company_key and add the identity keys it lacks; returns the leads re-keyed.

    For databases written before name normalisation understood accents and non-Latin scripts.
    Old keys are kept, so spellings seen before still merge. Needs a sqlite3.Row connection.
    """
    with _conn(conn) as c:
        rekeyed: list[Lead] = []
        for row in c.execute("SELECT * FROM leads").fetchall():
            lead = _lead_from_row(row)
            ckey = company_key(lead.lead_company, lead.company_domain)
            if ckey != row["company_key"]:
                c.execute("UPDATE leads SET company_key = ? WHERE id = ?", (ckey, lead.id))
                rekeyed.append(lead)
            _add_keys(c, lead.company_id, lead.id, lead_identity_keys(lead, lead.kind))
        for company_id in {lead.company_id for lead in rekeyed}:
            rescore_company(company_id, conn=c)
        return len(rekeyed)


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
    score, tier, reasons = result.score, result.tier, result.reasons
    if row["status"] == "disqualified":  # like an ICP disqualifier: off the top of score-sorted lists
        score, tier = min(score, DISQUALIFIED_MAX_SCORE), "cold"
        reasons = ["! Disqualified (pipeline status)", *reasons]
    last = max((p.occurred_at for p in points), default=None)
    # A lead that went cold may be alerted again when it next turns hot.
    c.execute(
        "UPDATE leads SET icp_score = ?, intent_score = ?, score = ?, tier = ?, score_reasons = ?, "
        "last_signal_at = ?, alerted_at = CASE WHEN ? = 'cold' THEN NULL ELSE alerted_at END WHERE id = ?",
        (result.icp_score, result.intent_score, score, tier, json.dumps(reasons),
         iso(last) if last else None, tier, lead_id),
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


def message_text(text: str) -> str:
    """A message body as stored: every line break as a single LF character, and the ends trimmed.

    Browsers submit a textarea's line breaks as CR LF, two characters, while its character counter (and LinkedIn)
    count one per line break: without this a connection note with line breaks that fits the account's limit in the
    dashboard would be stored longer than the limit, and never queued for the agent.
    """
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def create_message(lead_id: int, body: str, *, channel: str = "linkedin_dm", subject: str = "", step: int = 1,
                   generated_by: str = "template", status: str = "draft", direction: str = "outbound",
                   conn: sqlite3.Connection | None = None) -> Message:
    if channel not in MESSAGE_CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(MESSAGE_CHANNELS)}")
    if status not in MESSAGE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MESSAGE_STATUSES)}")
    if direction not in ("outbound", "inbound"):
        raise ValueError("direction must be 'outbound' or 'inbound'")
    body = message_text(body)
    if not body:
        raise ValueError("message body is empty")
    now = iso()
    with _conn(conn) as c:
        lead = get_lead(lead_id, conn=c)
        cur = c.execute(
            "INSERT INTO messages (company_id, lead_id, direction, channel, step, subject, body, status, generated_by, "
            "created_at, updated_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lead.company_id, lead_id, direction, channel, max(1, int(step)), subject.strip(), body, status,
             generated_by, now, now, now if status in ("sent", "replied", "received") else None),
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
    """Edit a message. Marking it sent/replied also advances the lead's pipeline status.

    Approving it here is a person's approval (or Claude's, for the user), so approved_via is cleared: only
    auto_approve_due records 'auto'. Setting an approved message back to 'draft' on purpose also holds it, so
    auto-approve never approves it again behind the user's back; a text edit that makes it a draft doesn't.
    The read and the write hold SQLite's write lock (BEGIN IMMEDIATE), like auto_approve_due: an edit made while
    auto-approve approves the draft sees the approval, so the new text is a draft again, never approved unread.
    """
    with _write_locked(conn) as c:
        msg = get_message(message_id, conn=c)
        updates: dict[str, Any] = {}
        if body is not None:
            if not message_text(body):
                raise ValueError("message body is empty")
            updates["body"] = message_text(body)
        if subject is not None:
            updates["subject"] = subject.strip()
        if status is not None:
            if status not in MESSAGE_STATUSES:
                raise ValueError(f"status must be one of {', '.join(MESSAGE_STATUSES)}")
            updates["status"] = status
            if status in ("approved", "draft"):
                updates["approved_via"] = ""
            if status == "draft" and msg.status == "approved" and msg.direction == "outbound":
                updates["auto_hold"] = 1  # "Back to drafts": the user (or Claude) wants another look first
            # 'replied' = sent, then answered: a message first recorded that way was sent too (it then counts
            # toward the connection-request limits, which count every request sent, by its sent_at).
            if (status == "sent" or (status == "replied" and msg.direction == "outbound")) and not msg.sent_at:
                updates["sent_at"] = iso()
        elif (msg.status == "approved" and msg.direction == "outbound"
              and (updates.get("body", msg.body), updates.get("subject", msg.subject)) != (msg.body, msg.subject)):
            # An approval covers the exact text (an AI agent may send approved LinkedIn messages as they are):
            # changed text is a draft again until someone approves it, e.g. with status="approved" in the same call.
            # It isn't held: like any edited draft, it waits a full window before auto-approve (if on) approves it.
            updates["status"] = "draft"
            updates["approved_via"] = ""
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


BULK_MESSAGES_MAX = 200  # the Drafts tab lists at most 200 messages
BULK_ACTIONS = {"approve": "approved", "skip": "skipped"}
BULK_NOT_FOUND = "not found"
BULK_NOT_DRAFT = "no longer a draft"
BULK_CHANGED = "edited since the page opened"
BULK_NOTE_TOO_LONG = "connection note too long"


def message_version(msg: Message) -> str:
    """A fingerprint of the text a reviewer sees. Bulk actions carry it, so an approval covers the text on the page
    and not one Claude or another tab saved after the page was opened."""
    text = "\0".join((msg.channel, msg.subject, msg.body))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def bulk_update_drafts(company_id: int, selected: list[tuple[int, str]], action: str,
                       conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Approve or skip several drafts of one company in one transaction.

    `selected` holds (message id, message_version) pairs. A message is changed only while it is still an outbound
    draft of this company with the same text; an approval also needs a connection note that fits the company's
    LinkedIn account. Returns {"done": [ids], "problems": {reason: count}}.
    """
    if action not in BULK_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(BULK_ACTIONS)}")
    if len(selected) > BULK_MESSAGES_MAX:
        raise ValueError(f"select at most {BULK_MESSAGES_MAX} messages at a time")
    done: list[int] = []
    problems: dict[str, int] = {}
    with _write_locked(conn) as c:
        company = get_company(company_id, conn=c)
        now = iso()
        seen: set[int] = set()
        for message_id, version in selected:
            if message_id in seen:
                continue
            seen.add(message_id)
            row = c.execute("SELECT * FROM messages WHERE id = ? AND company_id = ?",
                            (message_id, company_id)).fetchone()
            msg = _message_from_row(row) if row is not None else None
            if msg is None:
                problem = BULK_NOT_FOUND
            elif msg.direction != "outbound" or msg.status != "draft":
                problem = BULK_NOT_DRAFT
            elif message_version(msg) != version:
                problem = BULK_CHANGED
            elif (action == "approve" and msg.channel == "linkedin_connect"
                  and len(msg.body) > connect_note_limit(company)):
                problem = BULK_NOTE_TOO_LONG
            else:
                c.execute("UPDATE messages SET status = ?, approved_via = '', updated_at = ? "
                          "WHERE id = ? AND status = 'draft'", (BULK_ACTIONS[action], now, message_id))
                done.append(message_id)
                continue
            problems[problem] = problems.get(problem, 0) + 1
    return {"done": done, "problems": problems}


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


# A 'running' row older than this belongs to a scan that died with its process.
SCAN_STALE_AFTER = timedelta(minutes=15)


class ScanInProgress(RuntimeError):
    """Another scan of the company (dashboard, scheduler, CLI or Claude, in any process) is still running."""

    def __init__(self, company_id: int, run: ScanRun | None = None):
        self.company_id = company_id
        self.run = run
        since = f" (started from {run.trigger} at {iso(run.started_at)})" if run else ""
        super().__init__(f"a scan of company {company_id} is already running{since}; try again when it has finished")


def running_scan_run(company_id: int, now: datetime | None = None,
                     conn: sqlite3.Connection | None = None) -> ScanRun | None:
    """The company's scan that is running now; a stale 'running' row doesn't count."""
    cutoff = iso((now or utcnow()) - SCAN_STALE_AFTER)
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM scan_runs WHERE company_id = ? AND status = 'running' AND started_at > ? "
                        "ORDER BY started_at DESC, id DESC LIMIT 1", (company_id, cutoff)).fetchone()
    return _scan_from_row(row) if row else None


def start_scan_run(company_id: int, trigger: str = "manual", conn: sqlite3.Connection | None = None) -> int:
    """Record a new running scan, or raise ScanInProgress while another one runs.

    Check and insert are one statement (SQLite holds the write lock for all of it), so two
    processes can never both start a scan of the same company.
    """
    now = utcnow()
    with _conn(conn) as c:
        cur = c.execute(
            "INSERT INTO scan_runs (company_id, trigger, status, started_at) SELECT ?, ?, 'running', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM scan_runs WHERE company_id = ? AND status = 'running' AND started_at > ?)",
            (company_id, trigger, iso(now), company_id, iso(now - SCAN_STALE_AFTER)))
        if cur.rowcount != 1:
            raise ScanInProgress(company_id, running_scan_run(company_id, now, conn=c))
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
# Hot-lead alerts
# --------------------------------------------------------------------------------------


def claim_new_hot_leads(company_id: int, conn: sqlite3.Connection | None = None) -> list[Lead]:
    """Hot people not alerted yet, stamped as alerted in the same statement.

    Leads turn hot in scans, through Claude, the API, CSV imports and edits; whichever process
    asks first gets each lead exactly once, so alerts and auto-drafts never go out twice.
    """
    with _conn(conn) as c:
        ids = [r["id"] for r in c.execute(
            "UPDATE leads SET alerted_at = ? WHERE company_id = ? AND kind = 'person' AND tier = 'hot' "
            "AND alerted_at IS NULL RETURNING id", (iso(), company_id)).fetchall()]
        if not ids:
            return []
        rows = c.execute(f"SELECT * FROM leads WHERE id IN ({', '.join('?' * len(ids))}) ORDER BY score DESC, id",
                         ids).fetchall()
    return [_lead_from_row(r) for r in rows]


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
        # Replies leads sent us (log_reply) are inbound messages; the statuses above are our outbound ones.
        messages["received"] = count("SELECT COUNT(*) FROM messages WHERE company_id = ? AND direction = 'inbound'",
                                     company_id)

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
            "new_people_7d": count("SELECT COUNT(*) FROM leads WHERE company_id = ? AND kind = 'person' "
                                   "AND created_at >= ?", company_id, iso(now - timedelta(days=7))),
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


# --------------------------------------------------------------------------------------
# AI agent sending: the approved-only LinkedIn send queue for an agent in the user's own browser
# --------------------------------------------------------------------------------------
#
# OpenBerry never drives LinkedIn. An MCP-capable browser agent the user runs asks for the queue, sends each
# message exactly as approved from the user's own logged-in browser and confirms it. Every guardrail is
# enforced here, so a confused or manipulated agent can't get past it: off unless the company turns it on,
# only messages a person approved, LinkedIn only, a rolling 24-hour limit, never leads who replied or are
# excluded, never a step twice, follow-ups only when due, and a kill switch the agent pulls on any problem.

AGENT_WINDOW = timedelta(hours=24)
AGENT_QUEUE_MAX = 50
MAX_AGENT_PAUSE_HOURS = 24 * 30
# Leads whose conversation a person handles: they replied, booked a call, closed, or were ruled out.
NO_AGENT_LEAD_STATUSES = ("replied", "meeting", "won", "lost", "disqualified")
# Sends counted toward the daily limit: the agent's confirmations, and LinkedIn messages Claude marked sent
# with update_message (so an agent can't send more by recording its sends the other way).
_AGENT_COUNTED_VIA = ("agent", "claude")
_AGENT_SENT_SQL = (f"company_id = ? AND direction = 'outbound' AND channel IN ({', '.join('?' * len(AGENT_CHANNELS))}) "
                   f"AND sent_via IN ({', '.join('?' * len(_AGENT_COUNTED_VIA))}) AND sent_at > ?")

# Connection requests have two more limits, on top of the daily one. Both count every connection request recorded
# as sent for the company, whoever sent it (the user's Mark sent, Claude, the agent), because LinkedIn counts every
# invitation and every note the account sends.
#
# Weekly: LinkedIn doesn't publish a weekly invitation number. About 100 a week, counted over the last 7 days, is
# widely reported by third parties, and reaching LinkedIn's limit blocks invitations for a week (LinkedIn help
# a550555). The agent stays well below it: at most 80 connection requests in any 7 days, on any account.
AGENT_WEEKLY_CONNECT_LIMIT = 80
CONNECT_WEEK = timedelta(days=7)
# Monthly, free accounts only: LinkedIn lets a free (Basic) account add a personal note to at most 5 invitations a
# month (LinkedIn help a563153, a6239760). Every connection request OpenBerry records carries a note, so the agent
# sends at most outreach.LINKEDIN_FREE_NOTES_PER_MONTH in any 30 days from a free account. Premium: no such limit.
NOTE_MONTH = timedelta(days=30)
_CONNECT_SENT_SQL = ("company_id = ? AND direction = 'outbound' AND channel = 'linkedin_connect' "
                     "AND sent_at IS NOT NULL AND sent_at > ?")


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def linkedin_profile_url(url: str) -> str:
    """The canonical https://www.linkedin.com/in/<slug>/ of a person's profile URL, or "" if `url` isn't one.

    Strict on purpose: the agent opens this URL in the user's logged-in browser and lead fields come from
    strangers, so only a linkedin.com host (or a subdomain) with an /in/<slug> path passes.
    """
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if (parsed.scheme.lower() not in ("http", "https") or parsed.username or parsed.password
            or port not in (None, 80, 443) or not (host == "linkedin.com" or host.endswith(".linkedin.com"))):
        return ""
    match = re.fullmatch(r"/in/([^/]+)/?", parsed.path)
    slug = unquote(match.group(1)) if match else ""
    if slug in ("", ".", "..") or any(ch.isspace() or ch in "/\\?#" or unicodedata.category(ch)[0] == "C"
                                      for ch in slug):
        return ""
    return f"https://www.linkedin.com/in/{quote(slug, safe='-_.~')}/"


def _unapprove_for_new_profile(c: sqlite3.Connection, lead_id: int) -> int:
    """The lead's LinkedIn profile changed (or the lead left the pipeline): its approved LinkedIn messages go
    back to draft.

    An approval covers the recipient too. Without this, changing a lead's linkedin_url (Claude, a merge, an
    import, a script) would make the agent send an approved text to a different person than the one approved.
    They are held as well, so auto-approve doesn't approve them again on its own: the user decides. So are the
    lead's LinkedIn drafts: they were written for the old profile too, or waited while the lead was out of the
    pipeline, and auto-approve would otherwise approve them at once, without a new review window.
    """
    marks = ", ".join("?" * len(AGENT_CHANNELS))
    cur = c.execute(f"UPDATE messages SET status = 'draft', approved_via = '', auto_hold = 1, updated_at = ? "
                    f"WHERE lead_id = ? AND direction = 'outbound' AND status = 'approved' AND channel IN ({marks})",
                    (iso(), lead_id, *AGENT_CHANNELS))
    c.execute(f"UPDATE messages SET auto_hold = 1 WHERE lead_id = ? AND direction = 'outbound' AND status = 'draft' "
              f"AND channel IN ({marks})", (lead_id, *AGENT_CHANNELS))
    return cur.rowcount


def _agent_sent_since(c: sqlite3.Connection, company_id: int, since: datetime) -> list[sqlite3.Row]:
    return c.execute(f"SELECT sent_via, sent_at FROM messages WHERE {_AGENT_SENT_SQL} ORDER BY sent_at",
                     (company_id, *AGENT_CHANNELS, *_AGENT_COUNTED_VIA, iso(since))).fetchall()


def _connect_state(c: sqlite3.Connection, company: Company, now: datetime) -> dict[str, Any]:
    """The connection-request limits: sent in the last 7 and 30 days, what's left, and whether they block."""
    times = [r["sent_at"] for r in c.execute(f"SELECT sent_at FROM messages WHERE {_CONNECT_SENT_SQL} ORDER BY sent_at",
                                             (company.id, iso(now - NOTE_MONTH)))]
    week_start = iso(now - CONNECT_WEEK)
    week = [t for t in times if t > week_start]
    notes_max = monthly_note_limit(company)  # 5 on a free account, None on Premium
    weekly_left = max(0, AGENT_WEEKLY_CONNECT_LIMIT - len(week))
    notes_left = None if notes_max is None else max(0, notes_max - len(times))
    blocked, frees = "", []
    # A slot frees up once enough of the counted requests are older than the window (as for the daily limit).
    if notes_max is not None and notes_left == 0:
        blocked = "monthly_note_limit"
        frees.append(datetime.fromisoformat(times[len(times) - notes_max]) + NOTE_MONTH)
    if weekly_left == 0:
        blocked = blocked or "weekly_connect_limit"
        frees.append(datetime.fromisoformat(week[len(week) - AGENT_WEEKLY_CONNECT_LIMIT]) + CONNECT_WEEK)
    return {
        "linkedin_account": company.outreach.linkedin_account,
        "connect_note_max_chars": connect_note_limit(company),
        "connect_sent_7d": len(week),
        "weekly_connect_limit": AGENT_WEEKLY_CONNECT_LIMIT,
        "connect_notes_30d": len(times),
        "monthly_note_limit": notes_max,
        "connect_remaining": weekly_left if notes_left is None else min(weekly_left, notes_left),
        "connect_blocked_reason": blocked,
        "connect_frees_at": iso(max(frees)) if frees else None,
    }


def connect_blocked_message(state: dict[str, Any], ahead: int = 0) -> str:
    """Why the agent may not send a connection request now ("" when it may), for the agent and the user.

    `ahead`: connection requests already in this queue, which take the slots that are left.
    """
    reason = state.get("connect_blocked_reason") or ""
    if not reason and ahead and ahead >= state.get("connect_remaining", 0):
        notes_left = (None if state.get("monthly_note_limit") is None
                      else state["monthly_note_limit"] - state["connect_notes_30d"])
        weekly_left = state["weekly_connect_limit"] - state["connect_sent_7d"]
        reason = "monthly_note_limit" if notes_left is not None and notes_left <= weekly_left else "weekly_connect_limit"
    if not reason:
        return ""
    queued = f", and the {ahead} ahead in this queue take the rest" if ahead else ""
    when = (f" The next one is allowed from {state['connect_frees_at']}."
            if state.get("connect_frees_at") and not ahead else "")
    if reason == "monthly_note_limit":
        return (f"free LinkedIn accounts can add a note to only {state['monthly_note_limit']} connection requests a "
                f"month: {state['connect_notes_30d']} were sent in the last 30 days, yours included{queued}, so your "
                f"agent sends no more connection requests for now.{when} Send the rest yourself without a note, or "
                "set the company's LinkedIn account to Premium (company profile, Outreach) if you have Premium. "
                "LinkedIn messages still go out")
    return (f"at most {state['weekly_connect_limit']} connection requests go out in any 7 days, to stay below "
            f"LinkedIn's weekly invitation limit: {state['connect_sent_7d']} were sent in the last 7 days, yours "
            f"included{queued}.{when} Connection requests wait until older ones are 7 days old; LinkedIn messages "
            "still go out")


def _note_too_long(company: Company, chars: int) -> str:
    limit = connect_note_limit(company)
    if limit == LINKEDIN_CONNECT_LIMIT_FREE:
        return (f"the connection note is longer than {limit} characters ({chars}), the most a free LinkedIn account "
                f"allows (Premium: {LINKEDIN_CONNECT_LIMIT}). Shorten it and approve it again, or set the company's "
                "LinkedIn account to Premium (company profile, Outreach) if you have Premium")
    return f"the connection note is longer than {limit} characters ({chars}), the most LinkedIn allows"


def _connect_problem(c: sqlite3.Connection, company: Company, msg: Message, now: datetime) -> str:
    """Why this connection request may not go out now (its note's length, the weekly or monthly limit), read
    afresh; "" for other channels."""
    if msg.channel != "linkedin_connect":
        return ""
    if len(msg.body) > connect_note_limit(company):
        return _note_too_long(company, len(msg.body))
    return connect_blocked_message(_connect_state(c, company, now))


def _agent_state(c: sqlite3.Connection, company: Company, now: datetime) -> dict[str, Any]:
    """Whether the agent may send now, and why not: the queue's header."""
    cfg = company.outreach
    paused_until = cfg.agent_paused_until if cfg.agent_paused_until and _utc(cfg.agent_paused_until) > now else None
    sent = _agent_sent_since(c, company.id, now - AGENT_WINDOW)
    remaining = max(0, cfg.agent_daily_limit - len(sent))
    if not cfg.agent_sending:
        blocked = "disabled"
    elif paused_until is not None:
        blocked = "paused"
    elif remaining == 0:
        blocked = "daily_limit"
    else:
        blocked = ""
    # When the limit is reached, a slot frees up once the oldest counted send is 24 hours old.
    frees_at = (datetime.fromisoformat(sent[len(sent) - cfg.agent_daily_limit]["sent_at"]) + AGENT_WINDOW
                if remaining == 0 and sent else None)
    return {
        "enabled": cfg.agent_sending,
        "paused_until": iso(paused_until) if paused_until else None,
        "pause_reason": cfg.agent_pause_reason if paused_until else "",
        "daily_limit": cfg.agent_daily_limit,
        "sent_last_24h": len(sent),
        "remaining": remaining,
        "blocked_reason": blocked,
        "sent_by_agent_24h": sum(1 for r in sent if r["sent_via"] == "agent"),
        "marked_by_claude_24h": sum(1 for r in sent if r["sent_via"] == "claude"),
        "limit_frees_at": iso(frees_at) if frees_at else None,
        **_connect_state(c, company, now),
    }


def agent_blocked_message(state: dict[str, Any]) -> str:
    """Why the agent may not send now, in words for the agent and the user ("" when it may)."""
    if state["blocked_reason"] == "disabled":
        return ("AI agent sending is turned off for this company. Only the user can turn it on, in the dashboard "
                "(company settings, Outreach)")
    if state["blocked_reason"] == "paused":
        return (f"AI agent sending is paused until {state['paused_until']} after a reported problem "
                f"({state['pause_reason'] or 'no details'}). Only the user can resume it, in the dashboard")
    if state["blocked_reason"] == "daily_limit":
        when = f"; the next slot frees up at {state['limit_frees_at']}" if state["limit_frees_at"] else ""
        return (f"the daily limit of {state['daily_limit']} LinkedIn messages in 24 hours is reached "
                f"({state['sent_last_24h']} sent){when}. Stop sending for now")
    return ""


def _followup_wait_days(company: Company, step: int) -> int:
    days = company.outreach.followup_days or [3]
    return days[max(0, min(step - 1, len(days) - 1))]


LEAD_REPLIED = "the lead has replied, so answer them yourself"


def _lead_status_problem(lead: Lead) -> str:
    """Why nothing goes to this lead without a person: its status says a person handles the conversation, or that
    it was ruled out ("" if not). Shared by the agent's send queue and auto-approve."""
    if lead.status in NO_AGENT_LEAD_STATUSES:
        return f"the lead is marked {lead.status.capitalize()}"
    return ""


def _excluded_problem(company: Company, lead: Lead) -> str:
    """Why nothing goes to this lead: it is on the never-contact list or matches an excluded keyword ("" if not).

    Shared by the agent's send queue and auto-approve."""
    _, reasons, excluded = icp_fit(LeadFacts.from_obj(lead), company.icp)
    if excluded:
        reason = reasons[0].removeprefix("! ") if reasons else "never contact"
        return f"the lead is excluded by your ideal customer profile ({reason[:1].lower()}{reason[1:]})"
    return ""


def _sent_before_problem(msg: Message, sent: set[tuple[str, int]]) -> str:
    """Why this message would repeat one already sent to its lead ("" if not). `sent` holds the (channel, step) of the
    lead's other outbound messages that were sent. Shared by the agent's send queue and auto-approve."""
    if msg.channel == "linkedin_connect" and any(channel == "linkedin_connect" for channel, _ in sent):
        return "a connection request was already sent to this lead"
    if (msg.channel, msg.step) in sent:
        return f"step {msg.step} was already sent to this lead on {msg.channel}"
    return ""


def _agent_send_problem(c: sqlite3.Connection, company: Company, msg: Message, lead: Lead, now: datetime) -> str:
    """Why the agent must not send this message, or "" if it may (status and company switches aside)."""
    if msg.direction != "outbound":
        return "it is not an outbound message"
    if msg.sent_at is not None:  # e.g. sent, then set back to approved: never again, and never uncounted
        return f"it was already recorded as sent on {iso(msg.sent_at)}: a message is never sent twice"
    if msg.channel not in AGENT_CHANNELS:
        return f"{msg.channel} messages are never sent by the agent (LinkedIn only): send it yourself"
    if lead.company_id != company.id:
        return "the lead belongs to another company"
    if lead.kind != "person":
        return "the lead is a company with no contact person"
    if problem := _lead_status_problem(lead):
        return problem
    thread = [_message_from_row(r) for r in c.execute("SELECT * FROM messages WHERE lead_id = ? ORDER BY id",
                                                       (lead.id,))]
    if any(m.direction == "inbound" for m in thread):
        return LEAD_REPLIED
    if problem := _excluded_problem(company, lead):
        return problem
    if not linkedin_profile_url(lead.linkedin_url):
        return "the lead has no LinkedIn profile URL (https://www.linkedin.com/in/...)"
    if msg.channel == "linkedin_connect" and len(msg.body) > connect_note_limit(company):
        return _note_too_long(company, len(msg.body))
    # Sent = marked sent or replied, or sent once and set back to another status later (sent_at stays).
    sent = [m for m in thread if m.id != msg.id and m.direction == "outbound"
            and (m.status in ("sent", "replied") or m.sent_at is not None)]
    if problem := _sent_before_problem(msg, {(m.channel, m.step) for m in sent}):
        return problem
    sent_times = [_utc(m.sent_at) for m in sent if m.sent_at]
    if sent_times:
        due = max(sent_times) + timedelta(days=_followup_wait_days(company, max(m.step for m in sent)))
        if due > now:
            return f"the next message to this lead is not due before {iso(due)} (follow-up wait)"
    return ""


def lead_counted_sends(lead_id: int, now: datetime | None = None, conn: sqlite3.Connection | None = None) -> int:
    """How many of the lead's messages count toward its company's agent limits right now (deleting the lead
    would free those slots): the daily limit, and the connection-request limits (7 days, or 30 on a free
    LinkedIn account, whoever sent them)."""
    now = _utc(now or utcnow())
    marks = ", ".join("?" * len(AGENT_CHANNELS))
    with _conn(conn) as c:
        lead = get_lead(lead_id, conn=c)
        connect_window = CONNECT_WEEK if monthly_note_limit(get_company(lead.company_id, conn=c)) is None else NOTE_MONTH
        return c.execute(f"SELECT COUNT(*) FROM messages WHERE lead_id = ? AND direction = 'outbound' AND ("
                         f"(channel IN ({marks}) AND sent_via IN ({', '.join('?' * len(_AGENT_COUNTED_VIA))}) "
                         f"AND sent_at > ?) OR (channel = 'linkedin_connect' AND sent_at > ?))",
                         (lead_id, *AGENT_CHANNELS, *_AGENT_COUNTED_VIA, iso(now - AGENT_WINDOW),
                          iso(now - connect_window))).fetchone()[0]


def agent_sending_status(company_id: int, now: datetime | None = None,
                         conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """send_queue's header without the items: on/off, pause, limit, sends in the last 24 hours, blocked_reason."""
    with _conn(conn) as c:
        return _agent_state(c, get_company(company_id, conn=c), _utc(now or utcnow()))


def send_queue(company_id: int, limit: int = 10, now: datetime | None = None,
               conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The LinkedIn messages an AI agent may send now, exactly as approved, oldest first.

    Empty with a blocked_reason when agent sending is off ("disabled"), paused after a reported problem
    ("paused") or the rolling 24-hour limit is used up ("daily_limit"). Holds at most one message per lead
    and never more than the remaining daily allowance. Connection requests also stay within the weekly
    limit (AGENT_WEEKLY_CONNECT_LIMIT in any 7 days) and, on a free LinkedIn account, the monthly note
    limit (5 in any 30 days): beyond them connection requests are left out, with connect_blocked_reason
    ("weekly_connect_limit" or "monthly_note_limit") and a reason in `skipped`, while LinkedIn messages
    still flow. Approved LinkedIn messages that may not be sent are listed in `skipped` with the reason.
    Reading the queue changes nothing.
    """
    now = _utc(now or utcnow())
    limit = max(1, min(int(limit), AGENT_QUEUE_MAX))
    with _conn(conn) as c:
        company = get_company(company_id, conn=c)
        state = _agent_state(c, company, now)
        out: dict[str, Any] = {**state, "items": [], "skipped": [], "eligible_total": 0}
        if state["blocked_reason"]:
            return out
        rows = c.execute(
            f"SELECT * FROM messages WHERE company_id = ? AND direction = 'outbound' AND status = 'approved' "
            f"AND channel IN ({', '.join('?' * len(AGENT_CHANNELS))}) ORDER BY created_at, id",
            (company_id, *AGENT_CHANNELS)).fetchall()
        leads: dict[int, Lead] = {}
        queued_leads: set[int] = set()
        connects = 0  # connection requests queued so far: they take the weekly / monthly slots that are left
        for row in rows:
            msg = _message_from_row(row)
            if msg.lead_id not in leads:
                leads[msg.lead_id] = get_lead(msg.lead_id, conn=c)
            lead = leads[msg.lead_id]
            problem = _agent_send_problem(c, company, msg, lead, now)
            if not problem and lead.id in queued_leads:
                problem = "another message to this lead is ahead in the queue (one message per lead at a time)"
            if not problem and msg.channel == "linkedin_connect" and connects >= state["connect_remaining"]:
                problem = connect_blocked_message(state, ahead=connects)
                queued_leads.add(lead.id)  # the lead's connection request goes first, when a slot frees up
            if problem:
                out["skipped"].append({"message_id": msg.id, "lead_id": lead.id, "reason": problem})
                continue
            queued_leads.add(lead.id)
            connects += msg.channel == "linkedin_connect"
            out["eligible_total"] += 1
            if len(out["items"]) < min(limit, state["remaining"]):
                out["items"].append({
                    "message_id": msg.id, "lead_id": lead.id, "lead_name": lead.full_name, "lead_title": lead.title,
                    "lead_company": lead.lead_company, "linkedin_url": linkedin_profile_url(lead.linkedin_url),
                    "channel": msg.channel, "step": msg.step, "body": msg.body,
                })
        return out


def confirm_agent_sent(message_id: int, now: datetime | None = None,
                       conn: sqlite3.Connection | None = None) -> Message:
    """Record that the AI agent sent this approved LinkedIn message, exactly as approved.

    Refused (ValueError, with the reason) unless the company has agent sending on, isn't paused and is under
    its rolling 24-hour limit, and the message is approved and in the send queue; a connection request also
    needs a note within the account's length limit and a free slot in the weekly connection limit and, on a
    free LinkedIn account, the monthly note limit. Every check runs under SQLite's write lock (BEGIN
    IMMEDIATE), and the UPDATE repeats the limits, the never-sent and the duplicate-step checks itself, so two
    agents confirming at once can't both take the last slot or send the same step twice. Marks the message
    sent like update_message(status="sent") does, with sent_via="agent", and moves a new or qualified lead
    to 'contacted'. Confirming again a message the agent already confirmed returns it unchanged.
    """
    now = _utc(now or utcnow())
    with _write_locked(conn) as c:
        msg = get_message(message_id, conn=c)
        if msg.status == "sent" and msg.sent_via == "agent":
            return msg
        company = get_company(msg.company_id, conn=c)
        state = _agent_state(c, company, now)
        if state["blocked_reason"]:
            raise ValueError(f"not recorded: {agent_blocked_message(state)}")
        if msg.status != "approved":
            raise ValueError(f"not recorded: message {message_id} is '{msg.status}', not 'approved'. The agent only "
                             "sends messages the user approved, from get_send_queue")
        problem = _agent_send_problem(c, company, msg, get_lead(msg.lead_id, conn=c), now)
        if problem:
            raise ValueError(f"not recorded: message {message_id} is not in the send queue: {problem}")
        if msg.channel == "linkedin_connect" and state["connect_blocked_reason"]:
            raise ValueError(f"not recorded: {connect_blocked_message(state)}")
        stamp = iso(now)
        # The connection-request limits, repeated in the UPDATE like the daily limit: the note's length for the
        # account, the weekly limit and, on a free account, the monthly note limit.
        connect_sql, connect_args = "", []
        if msg.channel == "linkedin_connect":
            connect_sql = (f"AND length(body) <= ? AND (SELECT COUNT(*) FROM messages WHERE {_CONNECT_SENT_SQL}) < ? ")
            connect_args = [connect_note_limit(company), company.id, iso(now - CONNECT_WEEK),
                            AGENT_WEEKLY_CONNECT_LIMIT]
            notes_max = monthly_note_limit(company)
            if notes_max is not None:
                connect_sql += f"AND (SELECT COUNT(*) FROM messages WHERE {_CONNECT_SENT_SQL}) < ? "
                connect_args += [company.id, iso(now - NOTE_MONTH), notes_max]
        cur = c.execute(
            f"UPDATE messages SET status = 'sent', sent_at = ?, sent_via = 'agent', updated_at = ? "
            f"WHERE id = ? AND status = 'approved' AND sent_at IS NULL AND channel = ? "
            f"AND (SELECT COUNT(*) FROM messages WHERE {_AGENT_SENT_SQL}) < ? {connect_sql}"
            f"AND NOT EXISTS (SELECT 1 FROM messages o WHERE o.lead_id = ? AND o.id != ? AND o.direction = 'outbound' "
            f"AND (o.status IN ('sent', 'replied') OR o.sent_at IS NOT NULL) AND o.channel = ? "
            f"AND (o.step = ? OR o.channel = 'linkedin_connect'))",
            (stamp, stamp, message_id, msg.channel, company.id, *AGENT_CHANNELS, *_AGENT_COUNTED_VIA,
             iso(now - AGENT_WINDOW), company.outreach.agent_daily_limit, *connect_args,
             msg.lead_id, message_id, msg.channel, msg.step))
        if cur.rowcount != 1:  # another process got there first: it confirmed this message, or took the last slot
            again = get_message(message_id, conn=c)
            if again.status == "sent" and again.sent_via == "agent":
                return again
            if again.status != "approved":
                raise ValueError(f"not recorded: message {message_id} is now '{again.status}', not 'approved'")
            reason = agent_blocked_message(_agent_state(c, company, now)) or _agent_send_problem(
                c, company, again, get_lead(again.lead_id, conn=c), now) or _connect_problem(
                c, company, again, now) or (
                f"the daily limit of {company.outreach.agent_daily_limit} LinkedIn messages in 24 hours is reached. "
                "Stop sending for now")
            raise ValueError(f"not recorded: {reason}")
        c.execute("UPDATE leads SET status = 'contacted', updated_at = ? WHERE id = ? "
                  "AND status IN ('new', 'qualified')", (stamp, msg.lead_id))
        return get_message(message_id, conn=c)


def update_message_as(message_id: int, via: str, *, status: str | None = None, body: str | None = None,
                      subject: str | None = None, now: datetime | None = None,
                      conn: sqlite3.Connection | None = None) -> Message:
    """update_message, also recording who marked it sent (Message.sent_via) when this call marks it sent.

    The MCP server passes via="claude": LinkedIn messages Claude records as sent count toward the agent's
    daily limit, so recording a send this way never gets around it. That includes an outbound message first
    recorded as 'replied' (sent, then answered), which update_message dates as sent too.
    """
    if via not in ("agent", "claude"):
        raise ValueError("via must be 'agent' or 'claude'")
    with _write_locked(conn) as c:
        before = get_message(message_id, conn=c)
        updated = update_message(message_id, status=status, body=body, subject=subject, conn=c)
        if ((status == "sent" and before.status != "sent")
                or (status == "replied" and before.direction == "outbound" and before.sent_at is None)):
            stamp = iso(_utc(now or utcnow()))
            c.execute("UPDATE messages SET sent_via = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                      (via, stamp, stamp, message_id))
            updated = get_message(message_id, conn=c)
        return updated


def _save_agent_pause(c: sqlite3.Connection, company_id: int, until: datetime | None, reason: str) -> None:
    """Write only the pause fields of a company's outreach settings (no rescoring).

    Callers hold the write lock (_write_locked), as update_company does, so a profile save at the same
    moment can't undo the pause, and the pause can't undo the save.
    """
    row = c.execute("SELECT outreach FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise NotFound(f"company {company_id} not found")
    current = _loads(row["outreach"], {})
    outreach = {**(current if isinstance(current, dict) else {}),
                "agent_paused_until": iso(until) if until is not None else None,
                "agent_pause_reason": reason[:AGENT_PAUSE_REASON_MAX]}
    c.execute("UPDATE companies SET outreach = ?, updated_at = ? WHERE id = ?",
              (json.dumps(outreach), iso(), company_id))


NO_PROBLEM_DETAILS = "The agent reported a problem without details"


def report_send_problem(company_id: int, problem: str, message_id: int | None = None, hours: int = 24,
                        now: datetime | None = None, conn: sqlite3.Connection | None = None) -> Company:
    """The agent's kill switch: pause agent sending for the company and store the problem.

    It always pauses: an empty problem is stored as NO_PROBLEM_DETAILS, and a message_id that is unknown or
    belongs to another company never stops the pause (the other company, sent from the same browser, is
    paused too). The pause lasts `hours` (an existing longer pause is kept) and only the user lifts it early,
    in the dashboard. The message the agent was working on stays approved, or goes back to approved if the
    agent confirmed it in the last 24 hours, so nothing is lost; the pause reason then says so, because it
    may have gone out on LinkedIn: the user checks before resuming.
    Raises NotFound only when neither the company nor the message exists.
    """
    now = _utc(now or utcnow())
    text = " ".join(str(problem or "").split()) or NO_PROBLEM_DETAILS
    hours = max(1, min(int(hours), MAX_AGENT_PAUSE_HOURS))
    with _write_locked(conn) as c:
        row = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone() if message_id else None
        msg = _message_from_row(row) if row is not None else None
        targets = [cid for cid in dict.fromkeys([company_id, msg.company_id if msg else None]) if cid is not None
                   and c.execute("SELECT 1 FROM companies WHERE id = ?", (cid,)).fetchone()]
        if not targets:
            raise NotFound(f"company {company_id} not found")
        if (msg is not None and msg.direction == "outbound" and msg.status == "sent" and msg.sent_via == "agent"
                and msg.sent_at and _utc(msg.sent_at) > now - AGENT_WINDOW):
            c.execute("UPDATE messages SET status = 'approved', sent_at = NULL, sent_via = '', updated_at = ? "
                      "WHERE id = ?", (iso(now), msg.id))
            note = (f" [OpenBerry: message {msg.id} had been confirmed as sent and is approved again. Check on "
                    "LinkedIn whether it went out, and click Mark sent if it did, before you resume.]")
            text = text[:AGENT_PAUSE_REASON_MAX - len(note)] + note
        for cid in targets:
            current = get_company(cid, conn=c).outreach.agent_paused_until
            until = now + timedelta(hours=hours)
            if current is not None and _utc(current) > until:
                until = _utc(current)
            _save_agent_pause(c, cid, until, text)
        return get_company(targets[0], conn=c)


def resume_agent_sending(company_id: int, conn: sqlite3.Connection | None = None) -> Company:
    """Lift the agent's pause (the dashboard's Resume button). Agent sending stays on or off as it was."""
    with _write_locked(conn) as c:
        _save_agent_pause(c, company_id, None, "")
        return get_company(company_id, conn=c)


# --------------------------------------------------------------------------------------
# Auto-approve: drafts the user doesn't edit, hold or skip are approved once their review window has passed
# --------------------------------------------------------------------------------------
#
# Off unless the user turns it on for the company in the dashboard (outreach.auto_approve). A draft waits
# auto_approve_hours from when it was written or last edited, and at least that long after auto-approve was
# turned on (auto_approve_since), so turning it on never approves a backlog at once. With AI agent sending on,
# the user's browser agent may then send the approved LinkedIn messages, so auto-approve leaves alone whatever a
# person should look at first: drafts the user held, leads a person handles or must never contact, a connection
# note too long for the account, banned words, unfilled placeholders, an email without a subject, a step already
# sent, the first LinkedIn message after a connection request (did they accept?), and a second message to the same
# lead. A draft it leaves alone is flagged (messages.auto_blocked): once the reason goes away (the earlier message
# was sent or skipped, the lead set back, a banned word removed...), it waits a full review window from then, so
# nothing is approved the moment the reason goes away. send_queue and confirm_agent_sent still check every one of
# the agent's own rules afterwards.

AUTO_ONE_PER_LEAD_APPROVED = ("another message to this lead is approved and not sent yet (one message per lead at a "
                              "time)")
AUTO_ONE_PER_LEAD_OLDER = "an older draft to this lead goes first (one message per lead at a time)"
AUTO_COMPANY_PAUSED = "the company is paused"
AUTO_AFTER_CONNECT = ("it is the first LinkedIn message after your connection request: approve it yourself once they "
                      "accept")
AUTO_NO_SUBJECT = "the email has no subject line"


def _banned_words_used(company: Company, msg: Message) -> list[str]:
    """The company's banned words in a message: the check save_outreach_message applies to Claude's drafts."""
    from .collectors.base import find_terms  # the collectors import repo

    return find_terms(f"{msg.subject}\n{msg.body}", company.outreach.banned_words)


def _auto_approve_plan(c: sqlite3.Connection, company: Company, now: datetime,
                       lead_id: int | None = None) -> list[dict[str, Any]]:
    """What auto-approve does with each outbound draft of the company (or of one lead), oldest first.

    Each item: {"message", "version" (its stored updated_at), "state": "due" | "waiting" | "held" | "blocked",
    "at" (when its window ends), "reason" (why it is blocked), "write": what auto_approve_due stores, "block" (flag
    a newly blocked draft), "restart" (a flagged draft is clear now: its window starts again from `now`) or None}.
    Three queries, whatever the number of drafts.
    A lead's drafts are approved one at a time, in the order they were written: a newer draft waits while an
    older one is still a draft (waiting, held or blocked) or another message to the lead is approved and unsent.
    A draft repeating a step already sent doesn't hold up the lead's other drafts: it is stale.
    """
    where, params = "company_id = ? AND direction = 'outbound' AND status = 'draft'", [company.id]
    if lead_id is not None:
        where += " AND lead_id = ?"
        params.append(lead_id)
    rows = c.execute(f"SELECT * FROM messages WHERE {where} ORDER BY created_at, id", params).fetchall()
    if not rows:
        return []
    leads = {r["id"]: _lead_from_row(r) for r in c.execute(
        f"SELECT * FROM leads WHERE id IN (SELECT lead_id FROM messages WHERE {where})", params)}
    approved: set[int] = set()  # leads with a message approved and not sent yet
    replied: set[int] = set()
    sent: dict[int, list[tuple[int, str, int]]] = {}  # lead: (id, channel, step) of each outbound message sent
    for r in c.execute(f"SELECT id, lead_id, direction, channel, step, status, sent_at FROM messages "
                       f"WHERE lead_id IN (SELECT lead_id FROM messages WHERE {where}) AND (direction = 'inbound' "
                       f"OR status IN ('approved', 'sent', 'replied') OR sent_at IS NOT NULL)", params):
        if r["direction"] == "inbound":
            replied.add(r["lead_id"])
        elif r["status"] in ("sent", "replied") or r["sent_at"] is not None:
            sent.setdefault(r["lead_id"], []).append((r["id"], r["channel"], r["step"]))
        else:
            approved.add(r["lead_id"])
    cfg = company.outreach
    window = timedelta(hours=cfg.auto_approve_hours)
    since = _utc(cfg.auto_approve_since) if cfg.auto_approve_since else None
    note_max = connect_note_limit(company)
    lead_problems: dict[int, str] = {}
    older: set[int] = set()  # leads with an older draft earlier in this list
    plan: list[dict[str, Any]] = []
    for row in rows:
        msg = _message_from_row(row)
        start = _utc(msg.updated_at) if since is None else max(_utc(msg.updated_at), since)
        item: dict[str, Any] = {"message": msg, "version": row["updated_at"], "at": start + window, "reason": "",
                                "write": None}
        item["state"] = "due" if item["at"] <= now else "waiting"
        lead = leads.get(msg.lead_id)
        if lead is not None and lead.id not in lead_problems:
            lead_problems[lead.id] = (_lead_status_problem(lead) or (LEAD_REPLIED if lead.id in replied else "")
                                      or _excluded_problem(company, lead))
        sent_before = {(channel, step) for mid, channel, step in sent.get(msg.lead_id, []) if mid != msg.id}
        sent_channels = {channel for channel, _ in sent_before}
        repeat = _sent_before_problem(msg, sent_before)
        reason = ""
        if msg.auto_hold:
            item["state"] = "held"
        elif company.status != "active":
            reason = AUTO_COMPANY_PAUSED
        elif lead_problems.get(msg.lead_id):
            reason = lead_problems[msg.lead_id]
        elif repeat:
            reason = repeat
        elif msg.channel == "linkedin_connect" and len(msg.body) > note_max:
            reason = (f"the connection note has {len(msg.body)} characters, more than the {note_max} your "
                      f"{account_label(company)} LinkedIn account allows")
        elif banned := _banned_words_used(company, msg):
            reason = f"it uses a banned word or phrase ({', '.join(banned)})"
        elif placeholder := unfilled_placeholder(f"{msg.subject}\n{msg.body}"):
            reason = f"it still contains the placeholder '{placeholder}'"
        elif msg.channel == "email" and not msg.subject.strip():
            reason = AUTO_NO_SUBJECT
        elif msg.channel == "linkedin_dm" and sent_channels & set(AGENT_CHANNELS) == {"linkedin_connect"}:
            reason = AUTO_AFTER_CONNECT  # nothing records whether they accepted: the user checks
        elif msg.lead_id in approved:
            reason = AUTO_ONE_PER_LEAD_APPROVED
        elif msg.lead_id in older:
            reason = AUTO_ONE_PER_LEAD_OLDER
        if reason:
            item.update(state="blocked", reason=reason, write=None if msg.auto_blocked else "block")
        elif msg.auto_blocked and item["state"] in ("due", "waiting"):
            # The reason it was left alone went away: it waits a full window from now, like a draft just written.
            item.update(state="waiting", at=now + window, write="restart")
        if not repeat:
            older.add(msg.lead_id)
        plan.append(item)
    return plan


def auto_approve_due(company_id: int, now: datetime | None = None,
                     conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Approve the company's drafts whose review window has passed. Nothing while auto-approve is off or the
    company isn't active.

    The approvals are one write-locked transaction (BEGIN IMMEDIATE) that reads the drafts again. Each is a
    compare-and-set on the draft as it was read (still a draft, not held, the same updated_at), so an edit, a
    hold or a skip made at the same moment wins. It sets status 'approved' and approved_via 'auto'. The same
    transaction flags the drafts it newly leaves alone (auto_blocked) and starts a new window for flagged drafts
    whose reason went away. Without a caller's connection it looks first without the write lock, and takes it
    only when there is something to write: it runs on every page view of the Outreach page and lead pages, which
    mustn't wait for a scan's writes for nothing.
    It also runs on every scheduler tick, after each scan and before the agent's get_send_queue.
    Returns {"approved": [ids], "waiting": n, "held": n, "blocked": n, "next_at": when the next waiting draft
    is approved (iso), or None}.
    """
    now = _utc(now or utcnow())
    out: dict[str, Any] = {"approved": [], "waiting": 0, "held": 0, "blocked": 0, "next_at": None}

    def tally(plan: list[dict[str, Any]]) -> dict[str, Any]:
        for item in plan:
            out[item["state"]] += 1
        upcoming = [item["at"] for item in plan if item["state"] == "waiting"]
        out["next_at"] = iso(min(upcoming)) if upcoming else None
        return out

    if conn is None:
        with connect() as c:
            company = get_company(company_id, conn=c)
            if not company.outreach.auto_approve or company.status != "active":
                return out
            plan = _auto_approve_plan(c, company, now)
        if not any(item["state"] == "due" or item["write"] for item in plan):
            return tally(plan)
    with _write_locked(conn) as c:
        company = get_company(company_id, conn=c)
        if not company.outreach.auto_approve or company.status != "active":
            return out
        stamp = iso(now)
        plan = _auto_approve_plan(c, company, now)
        for item in plan:
            if item["write"] == "block":
                c.execute("UPDATE messages SET auto_blocked = 1 WHERE id = ? AND updated_at = ?",
                          (item["message"].id, item["version"]))
            elif item["write"] == "restart":
                c.execute("UPDATE messages SET auto_blocked = 0, updated_at = ? WHERE id = ? AND updated_at = ?",
                          (stamp, item["message"].id, item["version"]))
            if item["state"] != "due":
                continue
            cur = c.execute("UPDATE messages SET status = 'approved', approved_via = ?, updated_at = ? "
                            "WHERE id = ? AND status = 'draft' AND auto_hold = 0 AND updated_at = ?",
                            (APPROVED_VIA_AUTO, stamp, item["message"].id, item["version"]))
            if cur.rowcount == 1:
                out["approved"].append(item["message"].id)
                item["state"] = "approved"
            else:
                item["state"] = "waiting"  # changed at the same moment: it waits a new window
        return tally([item for item in plan if item["state"] != "approved"])


def auto_approve_states(company: Company, messages: list[Message], now: datetime | None = None,
                        conn: sqlite3.Connection | None = None) -> dict[int, dict[str, Any]]:
    """What auto-approve does with each outbound draft in `messages`, for the dashboard and Claude.

    {message_id: {"state": "waiting" | "held" | "blocked" | "off", "at": when it is approved (waiting), "reason":
    why it won't be (blocked)}}. Other messages are left out. The same few queries for any number of messages.
    """
    drafts = [m for m in messages if m.direction == "outbound" and m.status == "draft"]
    if not drafts:
        return {}
    if not company.outreach.auto_approve:
        return {m.id: {"state": "off", "at": None, "reason": ""} for m in drafts}
    lead_ids = {m.lead_id for m in drafts}
    with _conn(conn) as c:
        plan = _auto_approve_plan(c, company, _utc(now or utcnow()),
                                  lead_id=next(iter(lead_ids)) if len(lead_ids) == 1 else None)
    wanted = {m.id for m in drafts}
    return {
        item["message"].id: {"state": "waiting" if item["state"] == "due" else item["state"],
                             "at": item["at"] if item["state"] in ("due", "waiting") else None,
                             "reason": item["reason"]}
        for item in plan if item["message"].id in wanted
    }


def set_auto_hold(message_id: int, hold: bool = True, now: datetime | None = None,
                  conn: sqlite3.Connection | None = None) -> Message:
    """Hold an outbound draft, so auto-approve never approves it (the user approves it, or not, themselves), or
    release the hold (the dashboard only): the draft then waits a full review window again, from now.

    Holding a message auto-approve approved and nobody sent yet takes that approval back (a held draft again):
    the user clicked Hold on a page opened before the window ran out, and must not find it in the agent's queue.
    """
    with _write_locked(conn) as c:
        msg = get_message(message_id, conn=c)
        stamp = iso(_utc(now or utcnow()))
        if (hold and msg.direction == "outbound" and msg.status == "approved" and msg.approved_via == APPROVED_VIA_AUTO
                and msg.sent_at is None):
            c.execute("UPDATE messages SET status = 'draft', approved_via = '', auto_hold = 1, updated_at = ? "
                      "WHERE id = ?", (stamp, message_id))
            return get_message(message_id, conn=c)
        if msg.direction != "outbound" or msg.status != "draft":
            raise ValueError(f"only drafts can be {'held' if hold else 'released'}: message {message_id} is "
                             f"'{msg.status}'")
        if hold:
            c.execute("UPDATE messages SET auto_hold = 1 WHERE id = ?", (message_id,))
        else:
            c.execute("UPDATE messages SET auto_hold = 0, auto_blocked = 0, updated_at = ? WHERE id = ? "
                      "AND auto_hold = 1", (stamp, message_id))
        return get_message(message_id, conn=c)
