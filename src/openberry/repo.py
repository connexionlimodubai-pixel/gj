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
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from . import mailer
from .db import connect
from .models import (
    AGENT_CHANNELS,
    AGENT_PAUSE_REASON_MAX,
    APPROVED_VIA_AUTO,
    AUTO_APPROVE_SETTINGS,
    EMAIL_SETTINGS,
    LEAD_STATUSES,
    MESSAGE_CHANNELS,
    MESSAGE_STATUSES,
    PROSPECT_TYPES,
    SIGNAL_TYPES,
    Company,
    CompanyIn,
    Lead,
    LeadIn,
    Message,
    OutreachConfig,
    ScanRun,
    Signal,
    SignalIn,
)
from .outreach import (
    LINKEDIN_CONNECT_LIMIT,
    LINKEDIN_CONNECT_LIMIT_FREE,
    account_label,
    connect_note_limit,
    footer_problems,
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


# Google Maps links built from a Place ID alone: the only thing OpenBerry keeps from Google's Places API.
MAPS_PLACE_URL = "https://www.google.com/maps/place/?q=place_id:"
PLACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,300}")
_MAPS_LINK = re.compile(r"^https?://(?:www\.|maps\.)?google\.com/maps/[^#]*?[?&]"
                        r"(?:q=place_id(?::|%3A)|query_place_id=)([A-Za-z0-9_-]{10,300})(?=[&#]|$)", re.I)


def maps_place_url(place_id: str) -> str:
    """Google Maps link to a place, built from its Place ID."""
    return MAPS_PLACE_URL + place_id


def google_place_id(url: str) -> str:
    """The Place ID in a Google Maps link (place/?q=place_id:..., or the Maps URLs query_place_id=...), else ""."""
    m = _MAPS_LINK.match((url or "").strip())
    return m.group(1) if m else ""


def _norm_name(name: str) -> str:
    return " ".join(_words(_fold(name)))


def lead_identity_keys(data: LeadIn | Lead, kind: str) -> list[str]:
    keys: list[str] = []
    ckey = company_key(data.lead_company, data.company_domain)
    if kind == "account":
        # Both identities, so "Acme Bank" seen first by name and later with acme.com merge; and the Google Maps
        # Place ID, so the same business found again by another search merges too.
        keys = [f"acct:{k}" for k in (_name_key(data.lead_company), _domain_key(data.company_domain)) if k]
        if pid := google_place_id(data.profile_url):
            keys.append(f"acct:gp:{pid}")
        return keys
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
    auto-approve and email sending settings as stored, read under the same lock, so a profile save never turns
    them back on after the user turned them off. They change through a partial dict (the Outreach page's cards).
    The email pause (outreach.email_paused_until / email_pause_reason) is kept like the agent's: OpenBerry sets it
    (bounces, the mail provider refusing) and resume_email_sending lifts it.
    Activating a paused company with auto-approve on works like turning auto-approve on (auto_approve_since =
    now): the drafts that waited while it was paused get a full review window, never approved all at once.
    """
    with _write_locked(conn) as c:
        current = get_company(company_id, conn=c)
        kept = {key: getattr(current.outreach, key) for key in ("agent_paused_until", "agent_pause_reason",
                                                                 "email_paused_until", "email_pause_reason")}
        if isinstance(data, dict):
            merged = _deep_merge(current.model_dump(mode="json"), data)
            data = CompanyIn.model_validate(merged)
        else:
            kept.update({key: getattr(current.outreach, key) for key in (*AUTO_APPROVE_SETTINGS, *EMAIL_SETTINGS)})
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
    two John Smiths at Google with different LinkedIn profiles are two people. Likewise an account
    found on Google Maps never merges by name alone into one with another Place ID: two businesses.
    """
    strong = [k for k in keys if not k.startswith(_WEAK_KEY_PREFIXES)]
    if place := next((k for k in keys if k.startswith("acct:gp:")), ""):
        names = [k for k in strong if k.startswith("acct:n:")]
        strong = [k for k in strong if not k.startswith("acct:n:")]
        if (found := _lead_with_key(c, company_id, strong)) is not None:  # the same Place ID or domain
            return found
        if names:
            row = c.execute(
                f"SELECT MIN(k.lead_id) AS id FROM lead_keys k WHERE k.company_id = ? "
                f"AND k.key IN ({', '.join('?' * len(names))}) AND NOT EXISTS (SELECT 1 FROM lead_keys g "
                "WHERE g.company_id = k.company_id AND g.lead_id = k.lead_id "
                "AND substr(g.key, 1, 8) = 'acct:gp:' AND g.key != ?)", [company_id, *names, place]).fetchone()
            if row["id"] is not None:
                return row["id"]
    elif (found := _lead_with_key(c, company_id, strong)) is not None:
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
                if updates.get("email"):
                    _unapprove_emails(c, lead_id)  # the lead had no email address when approved
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
            if (normalize_email(merged.email) != normalize_email(lead.email)
                    or (merged.status in NO_AGENT_LEAD_STATUSES and lead.status not in NO_AGENT_LEAD_STATUSES)):
                _unapprove_emails(c, lead_id)  # an approval covers the address too, as it covers a LinkedIn profile
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
                 offset: int = 0, include_account: bool = False, exclude_types: Iterable[str] = (),
                 conn: sqlite3.Connection | None = None) -> tuple[list[Signal], int]:
    where = ["s.company_id = ?"]
    params: list[Any] = [company_id]
    if exclude := list(exclude_types):
        where.append(f"s.type NOT IN ({', '.join('?' * len(exclude))})")
        params.extend(exclude)
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


def log_reply(lead_id: int, body: str, *, channel: str = "linkedin_dm", generated_by: str = "lead",
              conn: sqlite3.Connection | None = None) -> Message:
    """Record a reply the lead sent us (pasted from LinkedIn or email by the user or Claude, or found in the inbox:
    generated_by "inbox"). Moves the lead to 'replied' and skips its drafts and approved messages."""
    with _conn(conn) as c:
        msg = create_message(lead_id, body, channel=channel, status="received", direction="inbound",
                             generated_by=generated_by, conn=c)
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
        edits = dict(updates)  # the text changes, before the status ones are added
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
        if msg.channel == "email" and msg.direction == "outbound":
            _email_edit_check(msg, edits, status, updates)
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
                if target in NO_AGENT_LEAD_STATUSES:  # the lead left the pipeline, as in update_lead
                    _unapprove_for_new_profile(c, lead.id)
        return get_message(message_id, conn=c)


def _email_edit_check(msg: Message, edits: dict[str, Any], status: str | None, updates: dict[str, Any]) -> None:
    """The email sending rules for an edit (update_message): nothing changes while the email is being sent, and the
    text of an email sent (or maybe sent) from the mailbox stays as it went out. A failed email that is edited or
    set back to draft starts afresh."""
    if msg.email_state in EMAIL_IN_FLIGHT:
        raise ValueError(EMAIL_BUSY)
    changed = (edits.get("body", msg.body), edits.get("subject", msg.subject)) != (msg.body, msg.subject)
    if changed and (msg.sent_via == "smtp" or msg.email_state in ("sent", "unknown", "bounced")):
        raise ValueError("This email was sent from your mailbox, or may have been, so its text can't be changed.")
    if msg.email_state == "failed" and (changed or status == "draft"):
        updates.update(email_state="", send_error="", email_attempts=0)
    if status == "sent" and msg.email_state == "unknown":  # the user saw it in their Sent folder
        updates.update(email_state="sent", sent_via="smtp")
        if msg.email_claimed_at and not msg.sent_at:
            updates["sent_at"] = iso(msg.email_claimed_at)


def delete_message(message_id: int, conn: sqlite3.Connection | None = None) -> None:
    with _write_locked(conn) as c:
        row = c.execute("SELECT email_state FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is not None and row["email_state"] in EMAIL_IN_FLIGHT:
            raise ValueError(EMAIL_BUSY)
        c.execute("DELETE FROM messages WHERE id = ?", (message_id,))


BULK_MESSAGES_MAX = 200  # the Drafts tab lists at most 200 messages
BULK_ACTIONS = {"approve": "approved", "skip": "skipped"}
BULK_NOT_FOUND = "not found"
BULK_NOT_DRAFT = "no longer a draft"
BULK_CHANGED = "edited since the page opened"
BULK_NOTE_TOO_LONG = "connection note too long"
BULK_NO_SUBJECT = "email without a subject"


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
    LinkedIn account, and an email with a subject. Returns {"done": [ids], "problems": {reason: count}}.
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
            elif action == "approve" and msg.channel == "email" and not msg.subject.strip():
                problem = BULK_NO_SUBJECT
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
# Paid API usage and Google Maps search state
# --------------------------------------------------------------------------------------

_ID_BATCH = 500  # SQLite host parameters per statement, well under its limit


@dataclass(frozen=True)
class ApiUsage:
    service: str
    month: str             # 'YYYY-MM' (UTC)
    used: int
    limit: int
    granted: bool = False  # reserve_api_call: this call may be made (and is counted already)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def usage_month(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(timezone.utc).strftime("%Y-%m")


def api_usage(service: str, limit: int, now: datetime | None = None,
              conn: sqlite3.Connection | None = None) -> ApiUsage:
    """This month's count, read-only (no row is created)."""
    month = usage_month(now)
    with _conn(conn) as c:
        row = c.execute("SELECT used FROM api_usage WHERE service = ? AND month = ?", (service, month)).fetchone()
    return ApiUsage(service, month, row["used"] if row else 0, limit)


def reserve_api_call(service: str, limit: int, now: datetime | None = None,
                     conn: sqlite3.Connection | None = None) -> ApiUsage:
    """Count one paid call before making it, unless that would go over `limit` this month.

    Atomic across threads and processes: the read and the write happen under SQLite's write lock (BEGIN IMMEDIATE).
    A call that then fails stays counted: Google may have billed it, and over-counting is the safe side. Only a
    refusal that is never billed is given back (release_api_call).
    """
    month = usage_month(now)
    with _write_locked(conn) as c:
        row = c.execute("SELECT used FROM api_usage WHERE service = ? AND month = ?", (service, month)).fetchone()
        used = row["used"] if row else 0
        if used >= limit:
            return ApiUsage(service, month, used, limit, granted=False)
        c.execute(
            "INSERT INTO api_usage (service, month, used, updated_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(service, month) DO UPDATE SET used = used + 1, updated_at = excluded.updated_at",
            (service, month, iso(now)))
        return ApiUsage(service, month, used + 1, limit, granted=True)


def release_api_call(service: str, month: str, conn: sqlite3.Connection | None = None) -> None:
    """Give back a call counted by reserve_api_call in `month` ('YYYY-MM'): the API refused it without running it
    (an invalid key, billing or the API turned off), so it was never billed."""
    with _write_locked(conn) as c:
        c.execute("UPDATE api_usage SET used = MAX(used - 1, 0), updated_at = ? WHERE service = ? AND month = ?",
                  (iso(), service, month))


def place_search_times(company_id: int, conn: sqlite3.Connection | None = None) -> dict[str, datetime]:
    """query_key -> when every page of that Google Maps search was last read."""
    with _conn(conn) as c:
        rows = c.execute("SELECT query_key, searched_at FROM place_searches WHERE company_id = ?",
                         (company_id,)).fetchall()
    return {r["query_key"]: datetime.fromisoformat(r["searched_at"]) for r in rows}


def finish_place_search(company_id: int, query_key: str, found: int, added: int, when: datetime | None = None,
                        conn: sqlite3.Connection | None = None) -> None:
    """Remember that every page of a Google Maps search was read (it then waits a week)."""
    with _conn(conn) as c:
        c.execute(
            "INSERT INTO place_searches (company_id, query_key, searched_at, found, added) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(company_id, query_key) DO UPDATE SET searched_at = excluded.searched_at, "
            "found = excluded.found, added = excluded.added",
            (company_id, query_key, iso(when), int(found), int(added)))


def prune_place_searches(company_id: int, keep: Iterable[str], conn: sqlite3.Connection | None = None) -> int:
    """Forget searches no longer in the profile, so adding one back runs it at once. Returns the rows deleted."""
    keep = set(keep)
    with _conn(conn) as c:
        gone = [r["query_key"] for r in c.execute("SELECT query_key FROM place_searches WHERE company_id = ?",
                                                  (company_id,)).fetchall() if r["query_key"] not in keep]
        c.executemany("DELETE FROM place_searches WHERE company_id = ? AND query_key = ?",
                      [(company_id, k) for k in gone])
    return len(gone)


def handled_place_ids(company_id: int, place_ids: Iterable[str], recheck_before: datetime,
                      conn: sqlite3.Connection | None = None) -> set[str]:
    """The given Place IDs to skip: they became a lead (added = 1), or were skipped since `recheck_before`."""
    ids = list(dict.fromkeys(place_ids))
    found: set[str] = set()
    with _conn(conn) as c:
        for start in range(0, len(ids), _ID_BATCH):
            batch = ids[start:start + _ID_BATCH]
            rows = c.execute(
                f"SELECT place_id FROM place_ids WHERE company_id = ? AND place_id IN ({', '.join('?' * len(batch))}) "
                "AND (added = 1 OR checked_at >= ?)", [company_id, *batch, iso(recheck_before)]).fetchall()
            found.update(r["place_id"] for r in rows)
    return found


def record_place_ids(company_id: int, outcomes: Mapping[str, bool], when: datetime | None = None,
                     conn: sqlite3.Connection | None = None) -> None:
    """Remember Place IDs this company handled: True when it became a lead (then it is never added again; written
    by services.ingest with the lead, so a scan that stops before storing its leads loses none), False when it was
    skipped."""
    if not outcomes:
        return
    stamp = iso(when)
    with _conn(conn) as c:
        c.executemany(
            "INSERT INTO place_ids (company_id, place_id, added, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(company_id, place_id) DO UPDATE SET added = MAX(added, excluded.added), "
            "checked_at = excluded.checked_at",
            [(company_id, pid, int(bool(added)), stamp) for pid, added in outcomes.items()])


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


_NOT_PROSPECT = f"type NOT IN ({', '.join(repr(t) for t in PROSPECT_TYPES)})"


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
        # Intent signals only: a business found on Google Maps (PROSPECT_TYPES) is a prospect, not intent.
        for r in c.execute("SELECT substr(occurred_at, 1, 10) d, COUNT(*) n FROM signals WHERE company_id = ? "
                           f"AND occurred_at >= ? AND {_NOT_PROSPECT} GROUP BY d", (company_id, start.isoformat())):
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
            "signals_7d": count(f"SELECT COUNT(*) FROM signals WHERE company_id = ? AND occurred_at >= ? "
                                f"AND {_NOT_PROSPECT}", company_id, iso(now - timedelta(days=7))),
            "businesses_7d": count(f"SELECT COUNT(*) FROM signals WHERE company_id = ? AND occurred_at >= ? "
                                   f"AND NOT {_NOT_PROSPECT}", company_id, iso(now - timedelta(days=7))),
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
# Auto-approve: drafts the user doesn't approve, hold or skip are approved once their review window has passed
# (an edit starts the window again)
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
AUTO_ONE_PER_LEAD_OLDER = "an earlier draft to this lead goes first (one message per lead at a time)"
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
    """What auto-approve does with each outbound draft of the company (or of one lead), in sequence order.

    Each item: {"message", "version" (its stored updated_at), "state": "due" | "waiting" | "held" | "blocked",
    "at" (when its window ends), "reason" (why it is blocked), "write": what auto_approve_due stores, "block" (flag
    a newly blocked draft), "restart" (a flagged draft is clear now: its window starts again from `now`) or None}.
    Three queries, whatever the number of drafts.
    A lead's drafts are approved one at a time, step by step, then in the order they were written: a draft waits
    while an earlier one (an earlier step, or the same step written before) is still a draft (waiting, held or
    blocked) or another message to the lead is approved and unsent. By step first: a rewritten connection note
    (a newer row) still goes before the step-2 message that follows it.
    A draft repeating a step already sent doesn't hold up the lead's other drafts: it is stale.
    """
    where, params = "company_id = ? AND direction = 'outbound' AND status = 'draft'", [company.id]
    if lead_id is not None:
        where += " AND lead_id = ?"
        params.append(lead_id)
    rows = c.execute(f"SELECT * FROM messages WHERE {where} ORDER BY step, created_at, id", params).fetchall()
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
    older: set[int] = set()  # leads with a draft earlier in this list
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


# --------------------------------------------------------------------------------------
# Email sending: OpenBerry sends the approved emails from the user's own mailbox (docs/EMAIL_SENDING.md)
# --------------------------------------------------------------------------------------
#
# Off unless the user connects a mailbox (the Mailbox page) and turns it on for the company (the Outreach page). Only
# the dashboard process sends (services.send_next_email): it claims one email here, sends it with mailer.send and
# records what happened. Every rule lives in this module and is checked under the write lock: approved emails only,
# exactly as approved plus the company's footer; auto-approved ones only with their own switch; the daily limit, the
# minutes between two emails, the sending hours and the mailbox's own cap; one email per lead at a time and
# follow-ups only when due; never a lead who replied or left the pipeline, the never-contact list, or an opted-out,
# bounced or no-reply address; never while the inbox check is stale; and never twice. An email is claimed, then marked
# "data" just before the server receives it, then recorded as sent: a connection lost during DATA leaves it
# "unknown", which is never sent again automatically (the user checks their Sent folder and decides). The inbox check
# (services.check_inbox) records replies, opt-outs and bounces through record_inbound.

EMAIL_WINDOW = timedelta(hours=24)
MAILBOX_DAILY_MAX = 100              # emails from the mailbox in any 24 hours, every company together
EMAIL_QUEUE_MAX = 100
EMAIL_CLAIM_STALE = timedelta(minutes=10)   # far longer than any SMTP step (mailer.TIMEOUT)
EMAIL_MAX_ATTEMPTS = 5
INBOX_FRESH = timedelta(minutes=30)  # nothing is sent unless the inbox was checked successfully this recently
EMAIL_PAUSE = timedelta(hours=24)
RETRY_PAUSES = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60))
BOUNCES_BEFORE_PAUSE = 3             # hard bounces for a company in 24 hours
POLICY_REJECTS_BEFORE_PAUSE = 2      # spam or policy rejections for a company in 24 hours
BOUNCE_LOOKBACK = timedelta(days=14)
REPLY_LOOKBACK = timedelta(days=180)
SEND_ERROR_MAX = 500
EMAIL_IN_FLIGHT = ("claimed", "data")
SUPPRESSION_REASONS = ("opt_out", "bounce", "manual")
# The time zone of the sending hours: None = the computer's own (tests set one).
EMAIL_LOCAL_TZ: tzinfo | None = None
# Counted toward the daily limits: emails OpenBerry sent, and emails being sent or that may have been sent.
_EMAIL_COUNTED = ("direction = 'outbound' AND channel = 'email' AND ((sent_via = 'smtp' AND sent_at > ?) "
                  "OR (email_state IN ('claimed', 'data', 'unknown') AND email_claimed_at > ?))")
EMAIL_BUSY = "This email is being sent right now. Try again in a minute."
EMAIL_ONE_AT_A_TIME = "another email to this lead goes first (one email per lead at a time)"
STOPPED_BEFORE_DATA = "OpenBerry stopped before sending it. It will try again."


def normalize_email(address: str) -> str:
    return (address or "").strip().lower()


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    return _utc(value) if isinstance(value, datetime) else _utc(datetime.fromisoformat(str(value)))


def local_time(now: datetime) -> datetime:
    """`now` in the time zone of the sending hours: this computer's own (EMAIL_LOCAL_TZ in tests)."""
    return now.astimezone(EMAIL_LOCAL_TZ) if EMAIL_LOCAL_TZ is not None else now.astimezone()


def in_sending_hours(cfg: OutreachConfig, now: datetime) -> bool:
    """Inside the company's sending hours (outreach.email_days, email_start_hour to email_end_hour, local time)."""
    local = local_time(_utc(now))
    return local.weekday() in cfg.email_days and cfg.email_start_hour <= local.hour < cfg.email_end_hour


def next_sending_time(cfg: OutreachConfig, now: datetime) -> datetime | None:
    """`now` when inside the sending hours, else when they next start (None when no day is chosen)."""
    now = _utc(now)
    if in_sending_hours(cfg, now):
        return now
    start = local_time(now).replace(minute=0, second=0, microsecond=0)
    for hours in range(1, 24 * 8):
        candidate = (start + timedelta(hours=hours)).astimezone(timezone.utc)
        if in_sending_hours(cfg, candidate):
            return candidate
    return None


def _mailbox_address(address: str | None) -> str:
    """The connected mailbox's From address: the one given, else the settings' ("" when none is connected)."""
    if address is not None:
        return normalize_email(address)
    from .config import get_settings

    return get_settings().mailbox_address()


_MAILBOX_DEFAULTS: dict[str, Any] = {"paused_until": None, "pause_reason": "", "auth_failed": 0, "failures": 0,
                                     "imap_state": "{}", "imap_checked_at": None, "imap_error": "",
                                     "imap_tried_at": None}


def _mailbox_row(c: sqlite3.Connection, address: str) -> dict[str, Any]:
    row = c.execute("SELECT * FROM email_mailbox WHERE address = ?", (address,)).fetchone()
    return dict(row) if row is not None else {"address": address, **_MAILBOX_DEFAULTS}


def _save_mailbox(c: sqlite3.Connection, address: str, now: datetime, **fields: Any) -> None:
    if not address:
        return
    row = {**_mailbox_row(c, address), **fields, "address": address, "updated_at": iso(now)}
    cols = ("address", *_MAILBOX_DEFAULTS, "updated_at")
    c.execute(f"INSERT INTO email_mailbox ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
              f"ON CONFLICT(address) DO UPDATE SET {', '.join(f'{k} = excluded.{k}' for k in cols[1:])}",
              [row[k] for k in cols])


def _mailbox_info(c: sqlite3.Connection, address: str, now: datetime) -> dict[str, Any]:
    """The mailbox's state for the queue's header and the dashboard. Never the server, username or password."""
    if not address:
        return {"connected": False, "address": "", "paused": False, "paused_until": None, "pause_reason": "",
                "auth_failed": False, "failures": 0, "inbox_checked_at": None, "inbox_ok": False, "inbox_error": "",
                "inbox_tried_at": None}
    row = _mailbox_row(c, address)
    until = _when(row["paused_until"])
    until = until if until is not None and until > now else None
    checked = _when(row["imap_checked_at"])
    return {
        "connected": True, "address": address,
        "paused": bool(row["auth_failed"]) or until is not None, "paused_until": iso(until) if until else None,
        "pause_reason": row["pause_reason"] if (row["auth_failed"] or until) else "",
        "auth_failed": bool(row["auth_failed"]), "failures": row["failures"],
        "inbox_checked_at": iso(checked) if checked else None,
        "inbox_ok": checked is not None and now - checked <= INBOX_FRESH and not row["imap_error"],
        "inbox_error": row["imap_error"], "inbox_tried_at": row["imap_tried_at"],
    }


def mailbox_state(address: str | None = None, now: datetime | None = None,
                  conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The connected mailbox's pause, failures and inbox check (address: the From address; default the settings')."""
    with _conn(conn) as c:
        return _mailbox_info(c, _mailbox_address(address), _utc(now or utcnow()))


def imap_positions(address: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Where the inbox check stopped in each folder: {"INBOX": {"uidvalidity", "last_uid"}, ...}."""
    with _conn(conn) as c:
        state = _loads(_mailbox_row(c, normalize_email(address))["imap_state"], {})
    return state if isinstance(state, dict) else {}


def save_inbox_check(address: str, ok: bool, error: str = "", positions: dict[str, Any] | None = None,
                     now: datetime | None = None, conn: sqlite3.Connection | None = None) -> None:
    """Record an inbox check: when it worked (sending needs one in the last INBOX_FRESH), or why it failed."""
    now = _utc(now or utcnow())
    fields: dict[str, Any] = {"imap_tried_at": iso(now),
                              "imap_error": "" if ok else (error or "failed")[:SEND_ERROR_MAX]}
    if ok:
        fields["imap_checked_at"] = iso(now)
    if positions is not None:
        fields["imap_state"] = json.dumps(positions)
    with _write_locked(conn) as c:
        _save_mailbox(c, normalize_email(address), now, **fields)


def retry_mailbox(address: str | None = None, now: datetime | None = None,
                  conn: sqlite3.Connection | None = None) -> None:
    """Lift the mailbox's pause (the dashboard's Try again, or saving new mailbox settings)."""
    now = _utc(now or utcnow())
    with _write_locked(conn) as c:
        _save_mailbox(c, _mailbox_address(address), now, paused_until=None, pause_reason="", auth_failed=0, failures=0)


def _email_times(c: sqlite3.Connection, company_id: int | None, since: datetime) -> list[str]:
    where = _EMAIL_COUNTED + (" AND company_id = ?" if company_id is not None else "")
    args: list[Any] = [iso(since), iso(since)] + ([company_id] if company_id is not None else [])
    return [r["t"] for r in c.execute(f"SELECT CASE WHEN sent_via = 'smtp' AND sent_at IS NOT NULL THEN sent_at "
                                      f"ELSE email_claimed_at END AS t FROM messages WHERE {where} ORDER BY t", args)]


def _last_email_attempt(c: sqlite3.Connection) -> datetime | None:
    """When the mailbox last started an email (any company, whatever happened): the minutes between two emails count
    from it."""
    return _when(c.execute("SELECT MAX(email_claimed_at) FROM messages "
                           "WHERE email_claimed_at IS NOT NULL").fetchone()[0])


def _email_state(c: sqlite3.Connection, company: Company, now: datetime, address: str) -> dict[str, Any]:
    """Whether OpenBerry may email for this company now, and why not: the email queue's header."""
    cfg = company.outreach
    mailbox = _mailbox_info(c, address, now)
    paused_until = _when(cfg.email_paused_until)
    paused_until = paused_until if paused_until is not None and paused_until > now else None
    times = _email_times(c, company.id, now - EMAIL_WINDOW)
    mailbox_count = len(_email_times(c, None, now - EMAIL_WINDOW)) if address else 0
    remaining = max(0, cfg.email_daily_limit - len(times))
    problems = footer_problems(company)
    in_hours = in_sending_hours(cfg, now)
    if not cfg.email_sending:
        blocked = "disabled"
    elif not mailbox["connected"]:
        blocked = "not_connected"
    elif mailbox["paused"]:
        blocked = "mailbox_paused"
    elif not mailbox["inbox_ok"]:
        blocked = "inbox_unchecked"
    elif problems:
        blocked = "footer"
    elif company.status != "active":
        blocked = "company_paused"
    elif paused_until is not None:
        blocked = "paused"
    elif remaining == 0:
        blocked = "daily_limit"
    elif mailbox_count >= MAILBOX_DAILY_MAX:
        blocked = "mailbox_limit"
    elif not in_hours:
        blocked = "outside_hours"
    else:
        blocked = ""
    frees_at = _when(times[len(times) - cfg.email_daily_limit]) + EMAIL_WINDOW if remaining == 0 and times else None
    last = _last_email_attempt(c)
    after_gap = last + timedelta(minutes=cfg.email_gap_minutes) if last is not None else None
    next_at: datetime | None = None
    if blocked in ("", "outside_hours"):
        next_at = next_sending_time(cfg, max(now, after_gap) if after_gap else now)
    elif blocked == "daily_limit" and frees_at is not None:
        next_at = next_sending_time(cfg, frees_at)
    return {
        "enabled": cfg.email_sending,
        "blocked_reason": blocked,
        "daily_limit": cfg.email_daily_limit,
        "sent_last_24h": len(times),
        "remaining": remaining,
        "limit_frees_at": iso(frees_at) if frees_at else None,
        "gap_minutes": cfg.email_gap_minutes,
        "next_send_at": iso(next_at) if next_at else None,
        "in_sending_hours": in_hours,
        "sending_days": list(cfg.email_days),
        "sending_hours": [cfg.email_start_hour, cfg.email_end_hour],
        "paused_until": iso(paused_until) if paused_until else None,
        "pause_reason": cfg.email_pause_reason if paused_until else "",
        "send_auto_approved": cfg.email_send_auto_approved,
        "footer": cfg.email_footer,
        "footer_problems": problems,
        "mailbox": mailbox,
        "mailbox_sent_24h": mailbox_count,
        "mailbox_daily_max": MAILBOX_DAILY_MAX,
    }


def email_blocked_message(state: dict[str, Any]) -> str:
    """Why OpenBerry sends no email for the company now, in plain words ("" when it may)."""
    reason = state.get("blocked_reason") or ""
    mailbox = state.get("mailbox") or {}
    if reason == "disabled":
        return ("Email sending is off for this company. Only the user can turn it on, on the dashboard's Outreach page "
                "(Email sending)")
    if reason == "not_connected":
        return "No mailbox is connected. The user connects one on the dashboard's Mailbox page"
    if reason == "mailbox_paused":
        return f"The mailbox is paused: {mailbox.get('pause_reason') or 'no details'}"
    if reason == "inbox_unchecked":
        error = mailbox.get("inbox_error")
        return ("Nothing is sent until OpenBerry has checked the inbox for replies and opt-outs in the last 30 minutes"
                + (f" (the last check failed: {error})" if error else ""))
    if reason == "footer":
        return "The email footer needs fixing: " + "; ".join(state.get("footer_problems") or [])
    if reason == "company_paused":
        return "The company is paused"
    if reason == "paused":
        return (f"Email sending is paused until {state['paused_until']} ({state['pause_reason'] or 'no details'}). "
                "Only the user can resume it early, on the dashboard")
    if reason == "daily_limit":
        when = f"; the next one can go out at {state['limit_frees_at']}" if state.get("limit_frees_at") else ""
        return f"The daily limit of {state['daily_limit']} emails in 24 hours is reached{when}"
    if reason == "mailbox_limit":
        return f"The mailbox sent {MAILBOX_DAILY_MAX} emails in the last 24 hours, the most OpenBerry sends from it"
    if reason == "outside_hours":
        when = f"; they start again at {state['next_send_at']}" if state.get("next_send_at") else ""
        return f"Outside the sending hours{when}"
    return ""


def _suppressions(c: sqlite3.Connection, addresses: Iterable[str]) -> dict[str, sqlite3.Row]:
    wanted = [a for a in dict.fromkeys(normalize_email(a) for a in addresses) if a]
    if not wanted:
        return {}
    rows = c.execute(f"SELECT * FROM email_suppressions WHERE email IN ({', '.join('?' * len(wanted))}) "
                     f"ORDER BY CASE reason WHEN 'opt_out' THEN 0 WHEN 'bounce' THEN 1 ELSE 2 END, id", wanted)
    out: dict[str, sqlite3.Row] = {}
    for row in rows:
        out.setdefault(row["email"], row)
    return out


def suppression_message(row: Any) -> str:
    when = str(row["created_at"])[:10]
    if row["reason"] == "opt_out":
        return f"{row['email']} asked not to be emailed ({when})"
    if row["reason"] == "bounce":
        return f"{row['email']} bounced on {when}: fix the address on the lead's page"
    return f"you marked {row['email']} as not to email ({when})"


def _never_contact_domain(company: Company, address: str) -> str:
    """The never-contact entry that names the address's domain (an entry that looks like a domain), or ""."""
    domain = mailer.domain_of(address)
    for entry in company.icp.exclude_companies:
        listed = normalize_domain(entry) if " " not in entry.strip() else ""
        if listed and (domain == listed or domain.endswith("." + listed)):
            return entry
    return ""


def _email_send_problem(company: Company, msg: Message, lead: Lead, thread: list[Message],
                        suppressed: dict[str, sqlite3.Row], now: datetime) -> str:
    """Why OpenBerry must not send this email now ("" if it may), the company-wide switches and limits aside."""
    if msg.direction != "outbound" or msg.channel != "email" or msg.status != "approved":
        return "it is not an approved email"
    if msg.sent_at is not None:
        return f"it was already recorded as sent on {iso(msg.sent_at)}: an email is never sent twice"
    if msg.email_state in EMAIL_IN_FLIGHT:
        return "it is being sent right now"
    if msg.email_state == "unknown":
        return ("it may have been sent already: check your mailbox's Sent folder, then choose Mark sent or Send "
                "again")
    if msg.email_state == "failed":
        return f"sending failed: {msg.send_error or 'no details'} Choose Try again once it is fixed"
    if msg.email_state == "bounced":
        return "it bounced"
    if msg.approved_via == APPROVED_VIA_AUTO and not company.outreach.email_send_auto_approved:
        return ("auto-approve approved it, and auto-approved emails aren't sent: approve it yourself, or allow them on "
                "the Email sending card")
    if not msg.subject.strip():
        return "it has no subject"
    if "\n" in msg.subject or "\r" in msg.subject:
        return "the subject has a line break"
    if lead.company_id != company.id:
        return "the lead belongs to another company"
    if problem := _lead_status_problem(lead):
        return problem
    if any(m.direction == "inbound" for m in thread):
        return LEAD_REPLIED
    if problem := _excluded_problem(company, lead):
        return problem
    address = normalize_email(lead.email)
    if not mailer.valid_address(address):
        return "the lead has no valid email address"
    if entry := _never_contact_domain(company, address):
        return f"the address's domain is on your never-contact list ({entry})"
    if mailer.no_reply_address(address):
        return "it is a no-reply or system address"
    if address in suppressed:
        return suppression_message(suppressed[address])
    sent = [m for m in thread if m.id != msg.id and m.direction == "outbound"
            and (m.status in ("sent", "replied") or m.sent_at is not None)]
    if any(m.channel == "email" and m.step == msg.step for m in sent):
        return f"step {msg.step} was already emailed to this lead"
    sent_times = [_utc(m.sent_at) for m in sent if m.sent_at]
    if sent_times:  # every message sent to the lead counts, on any channel
        due = max(sent_times) + timedelta(days=_followup_wait_days(company, max(m.step for m in sent)))
        if due > now:
            return f"the next message to this lead is not due before {iso(due)} (follow-up wait)"
    return ""


def _email_queue(c: sqlite3.Connection, company: Company, now: datetime, address: str,
                 limit: int = EMAIL_QUEUE_MAX) -> dict[str, Any]:
    state = _email_state(c, company, now, address)
    out: dict[str, Any] = {**state, "items": [], "skipped": [], "eligible_total": 0}
    rows = [_message_from_row(r) for r in c.execute(
        "SELECT * FROM messages WHERE company_id = ? AND direction = 'outbound' AND channel = 'email' "
        "AND status = 'approved' ORDER BY step, created_at, id", (company.id,))]
    if not rows:
        return out
    lead_ids = sorted({m.lead_id for m in rows})
    marks = ", ".join("?" * len(lead_ids))
    leads = {r["id"]: _lead_from_row(r) for r in c.execute(f"SELECT * FROM leads WHERE id IN ({marks})", lead_ids)}
    threads: dict[int, list[Message]] = {}
    for r in c.execute(f"SELECT * FROM messages WHERE lead_id IN ({marks}) ORDER BY id", lead_ids):
        threads.setdefault(r["lead_id"], []).append(_message_from_row(r))
    suppressed = _suppressions(c, [lead.email for lead in leads.values()])
    waiting: set[int] = set()  # leads with an earlier approved email not sent yet: theirs go one at a time
    eligible: list[tuple[Message, Lead]] = []
    for msg in rows:
        lead = leads.get(msg.lead_id)
        if lead is None:
            continue
        problem = _email_send_problem(company, msg, lead, threads.get(lead.id, []), suppressed, now)
        if not problem and lead.id in waiting:
            problem = EMAIL_ONE_AT_A_TIME
        if msg.sent_at is None:
            waiting.add(lead.id)
        if problem:
            out["skipped"].append({"message_id": msg.id, "lead_id": lead.id, "reason": problem,
                                   "state": msg.email_state})
            continue
        eligible.append((msg, lead))
    eligible.sort(key=lambda pair: (pair[0].created_at, pair[0].id))  # the oldest approved email first
    out["eligible_total"] = len(eligible)
    for msg, lead in eligible[:max(0, limit)]:
        out["items"].append({
            # A company lead's email goes to the company's own address (a hotel's reservations@): its name, as is.
            "message_id": msg.id, "lead_id": lead.id, "to": lead.email.strip(),
            "lead_name": lead.display_name if lead.kind == "person" else (lead.lead_company or lead.display_name),
            "subject": msg.subject, "step": msg.step, "auto_approved": msg.approved_via == APPROVED_VIA_AUTO,
        })
    return out


def email_queue(company_id: int, limit: int = 10, now: datetime | None = None, address: str | None = None,
                conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The approved emails OpenBerry may send for the company, oldest first, with the header (on/off, limits, pauses,
    the mailbox and its inbox check, blocked_reason) and `skipped`: approved emails held back, each with the reason.
    `items` lists what may go out once nothing blocks it (blocked_reason ""), at most `limit`. Reading it changes
    nothing."""
    now = _utc(now or utcnow())
    with _conn(conn) as c:
        return _email_queue(c, get_company(company_id, conn=c), now, _mailbox_address(address), limit)


def email_sending_status(company_id: int, now: datetime | None = None, address: str | None = None,
                         conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """email_queue's header without the items."""
    now = _utc(now or utcnow())
    with _conn(conn) as c:
        return _email_state(c, get_company(company_id, conn=c), now, _mailbox_address(address))


def _expire_email_claims(c: sqlite3.Connection, now: datetime) -> None:
    """Crash recovery. A claim older than EMAIL_CLAIM_STALE never reached DATA, so the email goes back to the queue
    (an attempt counted). One stuck in DATA may have been accepted by the server: it becomes "unknown"."""
    stale = iso(now - EMAIL_CLAIM_STALE)
    for row in c.execute("SELECT id, company_id, lead_id, email_state, email_attempts FROM messages "
                         "WHERE email_state IN ('claimed', 'data') AND email_claimed_at < ?", (stale,)).fetchall():
        if row["email_state"] == "claimed":
            attempts = row["email_attempts"] + 1
            failed = attempts >= EMAIL_MAX_ATTEMPTS
            c.execute("UPDATE messages SET email_state = ?, email_claim = '', email_attempts = ?, send_error = ? "
                      "WHERE id = ? AND email_state = 'claimed'",
                      ("failed" if failed else "", attempts,
                       f"Tried {attempts} times: {STOPPED_BEFORE_DATA}" if failed else STOPPED_BEFORE_DATA, row["id"]))
        else:
            text = mailer.UNKNOWN_TEXT.format(detail="OpenBerry closed or lost the connection")
            c.execute("UPDATE messages SET email_state = 'unknown', email_claim = '', send_error = ? WHERE id = ? "
                      "AND email_state = 'data'", (text, row["id"]))
            _email_event(c, row["company_id"], row["lead_id"], row["id"], "unknown", text, now=now)


def expire_email_claims(now: datetime | None = None, conn: sqlite3.Connection | None = None) -> None:
    """_expire_email_claims on its own (the email loop, a page that shows emails). The write lock is taken only when
    a claim is stale: a page view must not wait for a scan that holds it."""
    now = _utc(now or utcnow())
    if conn is None:
        with _conn(None) as c:
            if c.execute("SELECT 1 FROM messages WHERE email_state IN ('claimed', 'data') AND email_claimed_at < ? "
                         "LIMIT 1", (iso(now - EMAIL_CLAIM_STALE),)).fetchone() is None:
                return
    with _write_locked(conn) as c:
        _expire_email_claims(c, now)


def _company_last_email(c: sqlite3.Connection, company_id: int) -> str:
    return c.execute("SELECT MAX(email_claimed_at) FROM messages WHERE company_id = ?", (company_id,)).fetchone()[0] \
        or ""


def claim_next_email(claim: str, from_email: str, from_name: str = "", now: datetime | None = None,
                     conn: sqlite3.Connection | None = None) -> mailer.EmailJob | None:
    """Claim the next email the mailbox may send (any company), or None.

    One write-locked transaction: stale claims expire, then the mailbox (not paused, inbox checked in the last 30
    minutes, nothing in flight, under its own cap) and each company with email sending on (the one whose last email
    is oldest first: its switches, limits, sending hours and the minutes since the mailbox's last email) are checked,
    and the first email of its queue is claimed with a compare-and-set that repeats the limits and the never-twice
    checks. The returned EmailJob is everything the sending thread needs: it never reads the database again.
    """
    now = _utc(now or utcnow())
    address = normalize_email(from_email)
    with _write_locked(conn) as c:
        _expire_email_claims(c, now)
        mailbox = _mailbox_info(c, address, now)
        if not address or mailbox["paused"] or not mailbox["inbox_ok"]:
            return None
        if c.execute("SELECT 1 FROM messages WHERE email_state IN ('claimed', 'data') LIMIT 1").fetchone():
            return None
        last = _last_email_attempt(c)
        companies = [co for co in list_companies(conn=c) if co.outreach.email_sending and co.status == "active"]
        companies.sort(key=lambda co: (_company_last_email(c, co.id), co.id))
        for company in companies:
            if last is not None and now < last + timedelta(minutes=company.outreach.email_gap_minutes):
                continue
            queue = _email_queue(c, company, now, address)
            if queue["blocked_reason"]:
                continue
            for item in queue["items"]:
                job = _claim_email(c, company, item["message_id"], claim, from_email.strip(), from_name, now)
                if job is not None:
                    return job
        return None


def _claim_email(c: sqlite3.Connection, company: Company, message_id: int, claim: str, from_email: str,
                 from_name: str, now: datetime) -> mailer.EmailJob | None:
    msg = get_message(message_id, conn=c)
    lead = get_lead(msg.lead_id, conn=c)
    to = lead.email.strip()
    header_id = msg.email_message_id or mailer.new_header_id(from_email)
    since = iso(now - EMAIL_WINDOW)
    stamp = iso(now)
    cur = c.execute(
        f"UPDATE messages SET email_state = 'claimed', email_claim = ?, email_claimed_at = ?, email_to = ?, "
        f"email_message_id = ?, send_error = '' "
        f"WHERE id = ? AND direction = 'outbound' AND channel = 'email' AND status = 'approved' AND sent_at IS NULL "
        f"AND email_state = '' "
        f"AND NOT EXISTS (SELECT 1 FROM messages WHERE email_state IN ('claimed', 'data')) "
        f"AND (SELECT COUNT(*) FROM messages WHERE company_id = ? AND {_EMAIL_COUNTED}) < ? "
        f"AND (SELECT COUNT(*) FROM messages WHERE {_EMAIL_COUNTED}) < ? "
        f"AND NOT EXISTS (SELECT 1 FROM messages o WHERE o.lead_id = ? AND o.id != ? AND o.direction = 'outbound' "
        f"AND o.channel = 'email' AND o.step = ? AND (o.status IN ('sent', 'replied') OR o.sent_at IS NOT NULL)) "
        f"AND NOT EXISTS (SELECT 1 FROM messages i WHERE i.lead_id = ? AND i.direction = 'inbound') "
        f"AND NOT EXISTS (SELECT 1 FROM email_suppressions WHERE email = ?)",
        (claim, stamp, to, header_id, message_id, company.id, since, since, company.outreach.email_daily_limit,
         since, since, MAILBOX_DAILY_MAX, lead.id, message_id, msg.step, lead.id, normalize_email(to)))
    if cur.rowcount != 1:
        return None
    references = tuple(r["email_message_id"] for r in c.execute(
        "SELECT email_message_id FROM messages WHERE lead_id = ? AND id != ? AND sent_via = 'smtp' "
        "AND sent_at IS NOT NULL AND email_message_id IS NOT NULL ORDER BY sent_at, id", (lead.id, message_id)))
    return mailer.EmailJob(message_id=message_id, claim=claim, header_id=header_id, from_email=from_email,
                           from_name=from_name, to_email=to, to_name=lead.full_name if lead.kind == "person" else "",
                           subject=msg.subject, body=msg.body, footer=company.outreach.email_footer,
                           references=references)


def mark_email_data(message_id: int, claim: str, now: datetime | None = None,
                    conn: sqlite3.Connection | None = None) -> bool:
    """Just before DATA: record that the server is about to receive the email (state "data"), if it may still go.

    False (and the claim is released, nothing counted) when the claim isn't this worker's any more, or the email
    may no longer go: no longer approved (skipped, edited, the lead replied), email sending turned off or paused, the
    company paused, the address suppressed. The worker then sends RSET instead of DATA: nothing is sent.
    """
    now = _utc(now or utcnow())
    with _write_locked(conn) as c:
        row = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None or row["email_claim"] != claim or row["email_state"] != "claimed":
            return False
        msg = _message_from_row(row)
        company = get_company(msg.company_id, conn=c)
        cfg = company.outreach
        paused = cfg.email_paused_until is not None and _utc(cfg.email_paused_until) > now
        stop = (msg.status != "approved" or msg.sent_at is not None or not cfg.email_sending or paused
                or company.status != "active" or bool(_suppressions(c, [msg.email_to]))
                or c.execute("SELECT 1 FROM messages WHERE lead_id = ? AND direction = 'inbound' LIMIT 1",
                             (msg.lead_id,)).fetchone() is not None)
        if stop:
            c.execute("UPDATE messages SET email_state = '', email_claim = '' WHERE id = ? AND email_claim = ?",
                      (message_id, claim))
            return False
        cur = c.execute("UPDATE messages SET email_state = 'data' WHERE id = ? AND email_claim = ? "
                        "AND email_state = 'claimed'", (message_id, claim))
        return cur.rowcount == 1


def record_email_sent(message_id: int, header_id: str, address: str = "", now: datetime | None = None,
                      conn: sqlite3.Connection | None = None) -> Message:
    """The server accepted the email (250 after DATA): it is sent, sent_via "smtp".

    Keyed on its Message-ID, not on its status or the claim, so the truth is recorded even if the lead's reply
    skipped it meanwhile or the claim expired. Moves a new or qualified lead to contacted, and resets the mailbox's
    failures in a row.
    """
    now = _utc(now or utcnow())
    stamp = iso(now)
    with _write_locked(conn) as c:
        cur = c.execute("UPDATE messages SET status = 'sent', sent_at = ?, sent_via = 'smtp', email_state = 'sent', "
                        "email_claim = '', send_error = '', updated_at = ? WHERE id = ? AND email_message_id = ? "
                        "AND sent_at IS NULL", (stamp, stamp, message_id, header_id))
        msg = get_message(message_id, conn=c)
        if cur.rowcount == 1:
            c.execute("UPDATE leads SET status = 'contacted', updated_at = ? WHERE id = ? "
                      "AND status IN ('new', 'qualified')", (stamp, msg.lead_id))
            if address:
                _save_mailbox(c, normalize_email(address), now, failures=0)
        return msg


def record_email_failure(message_id: int, claim: str, kind: str, text: str, address: str = "",
                         now: datetime | None = None, conn: sqlite3.Connection | None = None) -> None:
    """What a failed send (mailer.Outcome.kind) does to the email, the mailbox and the company.

    aborted: back to the queue. auth / config: back to the queue, the mailbox paused until its settings are saved or
    the user clicks Try again. retry: back to the queue (failed after EMAIL_MAX_ATTEMPTS), the mailbox paused 5, 15,
    then 60 minutes. bounce: failed, the address never emailed again. provider_block: failed, the mailbox paused 24
    hours. failed: failed. unknown: never sent again automatically; the user decides. Bounces and policy rejections
    can pause the company's email sending for 24 hours (BOUNCES_BEFORE_PAUSE, POLICY_REJECTS_BEFORE_PAUSE).
    """
    now = _utc(now or utcnow())
    text = mailer.server_text(text)[:SEND_ERROR_MAX] if len(text) > SEND_ERROR_MAX else text
    address = normalize_email(address)
    with _write_locked(conn) as c:
        row = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        owned = row is not None and row["email_claim"] == claim and row["email_state"] in EMAIL_IN_FLIGHT

        def set_state(state: str, error: str, attempts: int | None = None) -> None:
            if owned:
                c.execute("UPDATE messages SET email_state = ?, email_claim = '', send_error = ?, "
                          "email_attempts = COALESCE(?, email_attempts) WHERE id = ?",
                          (state, error, attempts, message_id))

        company_id = row["company_id"] if row is not None else None
        lead_id = row["lead_id"] if row is not None else None
        mailbox = _mailbox_row(c, address) if address else None
        if kind == mailer.ABORTED:
            set_state("", "")
        elif kind in (mailer.AUTH, mailer.CONFIG):
            set_state("", "")
            _save_mailbox(c, address, now, auth_failed=1, paused_until=None, pause_reason=text)
        elif kind in (mailer.RETRY, mailer.UNKNOWN):
            if kind == mailer.RETRY:
                attempts = (row["email_attempts"] if row is not None else 0) + 1
                failed = attempts >= EMAIL_MAX_ATTEMPTS
                set_state("failed" if failed else "", f"Tried {attempts} times: {text}" if failed else text, attempts)
            else:
                set_state("unknown", text)
                if owned:
                    _email_event(c, company_id, lead_id, message_id, "unknown", text, now=now)
            if mailbox is not None:
                failures = mailbox["failures"] + 1
                pause = RETRY_PAUSES[min(failures, len(RETRY_PAUSES)) - 1]
                reason = f"{failures} attempts failed in a row: {text}" if failures > 1 else text
                _save_mailbox(c, address, now, failures=failures, paused_until=iso(now + pause), pause_reason=reason)
        elif kind == mailer.BOUNCE:
            set_state("failed", text)
            if owned:
                _add_suppression(c, row["email_to"], "bounce", company_id, lead_id, message_id, text, now)
                _tag_lead(c, lead_id, "email-bounced")
                _email_event(c, company_id, lead_id, message_id, "bounce", text, now=now)
                _email_guard(c, company_id, "bounce", now)
        elif kind == mailer.PROVIDER_BLOCK:
            set_state("failed", text)
            _save_mailbox(c, address, now, paused_until=iso(now + EMAIL_PAUSE), pause_reason=text)
            if owned:
                _email_event(c, company_id, lead_id, message_id, "policy_reject", text, now=now)
                _email_guard(c, company_id, "policy_reject", now)
        else:  # failed
            set_state("failed", text)
            if owned:
                _email_event(c, company_id, lead_id, message_id, "send_failed", text, now=now)


def resolve_email(message_id: int, action: str, now: datetime | None = None,
                  conn: sqlite3.Connection | None = None) -> Message:
    """The user's answer for an email that may have been sent ("unknown") or failed (the dashboard only).

    mark_sent: it went out (seen in the Sent folder): sent, from the mailbox. send_again: it didn't, so it goes back to
    the queue with the same Message-ID (it may arrive twice: the dashboard asks first). retry: a failed email goes back
    to the queue.
    """
    now = _utc(now or utcnow())
    with _write_locked(conn) as c:
        msg = get_message(message_id, conn=c)
        if msg.direction != "outbound" or msg.channel != "email":
            raise ValueError("only emails can be resolved")
        if action == "mark_sent" and msg.email_state == "unknown":
            stamp = iso(_utc(msg.email_claimed_at) if msg.email_claimed_at else now)
            c.execute("UPDATE messages SET status = 'sent', sent_at = COALESCE(sent_at, ?), sent_via = 'smtp', "
                      "email_state = 'sent', send_error = '', updated_at = ? WHERE id = ?",
                      (stamp, iso(now), message_id))
            c.execute("UPDATE leads SET status = 'contacted', updated_at = ? WHERE id = ? "
                      "AND status IN ('new', 'qualified')", (iso(now), msg.lead_id))
            _email_event(c, msg.company_id, msg.lead_id, message_id, "resolved", "You marked it sent", now=now)
        elif action == "send_again" and msg.email_state == "unknown" and msg.sent_at is None:
            c.execute("UPDATE messages SET email_state = '', send_error = '' WHERE id = ?", (message_id,))
            _email_event(c, msg.company_id, msg.lead_id, message_id, "resolved", "You chose Send again", now=now)
        elif action == "retry" and msg.email_state == "failed" and msg.sent_at is None:
            c.execute("UPDATE messages SET email_state = '', send_error = '', email_attempts = 0 WHERE id = ?",
                      (message_id,))
        else:
            raise ValueError("This email can't be changed that way any more. Reload the page.")
        return get_message(message_id, conn=c)


def _save_email_pause(c: sqlite3.Connection, company_id: int, until: datetime | None, reason: str) -> None:
    """Write only the email pause of a company's outreach settings, under the caller's write lock (as
    _save_agent_pause does), so a profile save at the same moment can't undo it."""
    row = c.execute("SELECT outreach FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise NotFound(f"company {company_id} not found")
    current = _loads(row["outreach"], {})
    outreach = {**(current if isinstance(current, dict) else {}),
                "email_paused_until": iso(until) if until is not None else None,
                "email_pause_reason": reason[:AGENT_PAUSE_REASON_MAX]}
    c.execute("UPDATE companies SET outreach = ?, updated_at = ? WHERE id = ?",
              (json.dumps(outreach), iso(), company_id))


def resume_email_sending(company_id: int, conn: sqlite3.Connection | None = None) -> Company:
    """Lift the company's email pause (the dashboard's Resume). Email sending stays on or off as it was."""
    with _write_locked(conn) as c:
        _save_email_pause(c, company_id, None, "")
        return get_company(company_id, conn=c)


def _email_guard(c: sqlite3.Connection, company_id: int | None, kind: str, now: datetime) -> None:
    """Too many hard bounces or policy rejections for a company in 24 hours pause its email sending for 24 hours."""
    if company_id is None:
        return
    limit = BOUNCES_BEFORE_PAUSE if kind == "bounce" else POLICY_REJECTS_BEFORE_PAUSE
    count = c.execute("SELECT COUNT(*) FROM email_events WHERE company_id = ? AND kind = ? AND created_at > ?",
                      (company_id, kind, iso(now - EMAIL_WINDOW))).fetchone()[0]
    if count >= limit:
        what = "hard bounces" if kind == "bounce" else "spam or policy rejections"
        _save_email_pause(c, company_id, now + EMAIL_PAUSE,
                          f"{count} {what} in 24 hours. Check the addresses you approve, and your domain's SPF, DKIM "
                          "and DMARC, before you resume.")


def _email_event(c: sqlite3.Connection, company_id: int | None, lead_id: int | None, message_id: int | None,
                 kind: str, detail: str = "", inbound_key: str | None = None, now: datetime | None = None) -> None:
    c.execute("INSERT OR IGNORE INTO email_events (company_id, lead_id, message_id, kind, detail, inbound_key, "
              "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (company_id, lead_id, message_id, kind, (detail or "")[:SEND_ERROR_MAX], inbound_key, iso(now)))


def list_email_events(company_id: int | None = None, lead_id: int | None = None, kinds: Iterable[str] = (),
                      since: datetime | None = None, limit: int = 50,
                      conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """What the email sender and the inbox check did, newest first."""
    where, args = ["1 = 1"], []
    if company_id is not None:
        where.append("company_id = ?")
        args.append(company_id)
    if lead_id is not None:
        where.append("lead_id = ?")
        args.append(lead_id)
    if kinds := list(kinds):
        where.append(f"kind IN ({', '.join('?' * len(kinds))})")
        args.extend(kinds)
    if since is not None:
        where.append("created_at > ?")
        args.append(iso(since))
    with _conn(conn) as c:
        rows = c.execute(f"SELECT * FROM email_events WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC "
                         "LIMIT ?", [*args, max(1, min(int(limit), 500))]).fetchall()
    return [{**dict(r), "created_at": _when(r["created_at"])} for r in rows]


def _tag_lead(c: sqlite3.Connection, lead_id: int | None, tag: str) -> None:
    if lead_id is None:
        return
    row = c.execute("SELECT tags FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        return
    tags = _loads(row["tags"], [])
    if tag not in tags:
        c.execute("UPDATE leads SET tags = ? WHERE id = ?", (json.dumps([*tags, tag]), lead_id))


def _add_suppression(c: sqlite3.Connection, address: str, reason: str, company_id: int | None = None,
                     lead_id: int | None = None, message_id: int | None = None, detail: str = "",
                     now: datetime | None = None) -> bool:
    address = normalize_email(address)
    if not address or "@" not in address:
        return False
    cur = c.execute("INSERT OR IGNORE INTO email_suppressions (email, reason, company_id, lead_id, message_id, detail, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (address, reason, company_id, lead_id, message_id, (detail or "")[:300], iso(now)))
    return cur.rowcount == 1


def add_suppression(address: str, reason: str = "manual", detail: str = "", company_id: int | None = None,
                    lead_id: int | None = None, conn: sqlite3.Connection | None = None) -> bool:
    """Never email this address again, for any company. True when it wasn't suppressed for that reason yet."""
    if reason not in SUPPRESSION_REASONS:
        raise ValueError(f"reason must be one of {', '.join(SUPPRESSION_REASONS)}")
    if not mailer.valid_address(normalize_email(address)):
        raise ValueError("enter a valid email address")
    with _write_locked(conn) as c:
        return _add_suppression(c, address, reason, company_id, lead_id, None, detail)


def remove_suppression(suppression_id: int, conn: sqlite3.Connection | None = None) -> None:
    with _write_locked(conn) as c:
        c.execute("DELETE FROM email_suppressions WHERE id = ?", (suppression_id,))


def list_suppressions(limit: int = 200, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM email_suppressions ORDER BY created_at DESC, id DESC LIMIT ?",
                         (max(1, min(int(limit), 1000)),)).fetchall()
    return [{**dict(r), "created_at": _when(r["created_at"])} for r in rows]


def suppressed_addresses(addresses: Iterable[str], conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
    """address (lower case) -> its suppression, for the addresses that have one."""
    with _conn(conn) as c:
        return {k: {**dict(v), "message": suppression_message(v)} for k, v in _suppressions(c, addresses).items()}


def _unapprove_emails(c: sqlite3.Connection, lead_id: int) -> int:
    """The lead's address changed, or the lead left the pipeline: its approved emails go back to draft, held, and its
    email drafts are held, so nothing goes to an address nobody approved (emails being sent right now aside)."""
    cur = c.execute("UPDATE messages SET status = 'draft', approved_via = '', auto_hold = 1, updated_at = ? "
                    "WHERE lead_id = ? AND direction = 'outbound' AND channel = 'email' AND status = 'approved' "
                    "AND sent_at IS NULL AND email_state NOT IN ('claimed', 'data')", (iso(), lead_id))
    c.execute("UPDATE messages SET auto_hold = 1 WHERE lead_id = ? AND direction = 'outbound' AND channel = 'email' "
              "AND status = 'draft'", (lead_id,))
    return cur.rowcount


def record_opt_out(lead_id: int, source: str, detail: str = "", message_id: int | None = None,
                   addresses: Iterable[str] = (), now: datetime | None = None, event: bool = True,
                   conn: sqlite3.Connection | None = None) -> Lead:
    """The lead asked not to be contacted: their address (and `addresses`) is never emailed again, for any company;
    the lead is disqualified, tagged "unsubscribed" and gets a dated note. A company lead (the address is the
    company's own) also puts the company on the never-contact list. Only restricts, so Claude may record one."""
    now = _utc(now or utcnow())
    with _write_locked(conn) as c:
        lead = get_lead(lead_id, conn=c)
        for address in {normalize_email(lead.email), *(normalize_email(a) for a in addresses)}:
            if address:
                _add_suppression(c, address, "opt_out", lead.company_id, lead.id, message_id, detail, now)
        tags = lead.tags + ["unsubscribed"] if "unsubscribed" not in lead.tags else lead.tags
        note = f"{now:%Y-%m-%d}: Asked not to be contacted ({source})."
        notes = lead.notes if note in lead.notes else f"{lead.notes}\n{note}".strip()
        if lead.kind == "account" and lead.lead_company.strip():
            company = get_company(lead.company_id, conn=c)
            listed = {e.casefold() for e in company.icp.exclude_companies}
            if lead.lead_company.casefold() not in listed:
                update_company(lead.company_id, {"icp": {"exclude_companies": [*company.icp.exclude_companies,
                                                                               lead.lead_company]}}, conn=c)
        updated = update_lead(lead_id, {"status": "disqualified", "tags": tags, "notes": notes}, conn=c)
        _unapprove_emails(c, lead_id)
        if event:
            _email_event(c, lead.company_id, lead.id, message_id, "opt_out", detail or source, now=now)
        return updated


def inbound_index(now: datetime | None = None, conn: sqlite3.Connection | None = None) -> dict[str, set[str]]:
    """What the inbox check looks for: the Message-IDs OpenBerry gave its emails, and the addresses of leads that got
    an email marked sent in the last REPLY_LOOKBACK (also by hand), so other mail is never read in full."""
    now = _utc(now or utcnow())
    with _conn(conn) as c:
        ours = {r[0] for r in c.execute("SELECT email_message_id FROM messages WHERE email_message_id IS NOT NULL "
                                        "AND email_state != ''")}
        leads = {normalize_email(r[0]) for r in c.execute(
            "SELECT DISTINCT l.email FROM leads l JOIN messages m ON m.lead_id = l.id WHERE m.direction = 'outbound' "
            "AND m.channel = 'email' AND m.sent_at > ? AND l.email != ''", (iso(now - REPLY_LOOKBACK),))}
    return {"ours": ours, "leads": leads}


def _match_inbound(c: sqlite3.Connection, inbound: mailer.Inbound, now: datetime) -> tuple[Message | None, Lead | None]:
    """The email and lead an inbox message is about: by the Message-IDs it refers to (a bounce: the email that
    bounced, else its failed address), the List-Unsubscribe token, or the sender being a lead we emailed."""
    bounce = inbound.kind in (mailer.BOUNCE, mailer.SOFT_BOUNCE, mailer.POLICY_REJECT)
    ids = list(dict.fromkeys([*inbound.references, *(inbound.original_ids if bounce else ())]))
    row = None
    if ids:
        row = c.execute(f"SELECT * FROM messages WHERE email_message_id IN ({', '.join('?' * len(ids))}) "
                        "ORDER BY COALESCE(sent_at, email_claimed_at) DESC, id DESC LIMIT 1", ids).fetchone()
    if row is None and bounce and inbound.failed_recipients:
        wanted = [normalize_email(a) for a in inbound.failed_recipients]
        row = c.execute(f"SELECT * FROM messages WHERE lower(email_to) IN ({', '.join('?' * len(wanted))}) "
                        "AND direction = 'outbound' AND email_state IN ('sent', 'unknown', 'bounced') "
                        "AND COALESCE(sent_at, email_claimed_at) > ? ORDER BY COALESCE(sent_at, email_claimed_at) DESC "
                        "LIMIT 1", [*wanted, iso(now - BOUNCE_LOOKBACK)]).fetchone()
    if row is None and inbound.token:
        row = c.execute("SELECT * FROM messages WHERE email_message_id = ? OR email_message_id LIKE ? LIMIT 1",
                        (f"<{inbound.token}>", f"<{inbound.token}@%")).fetchone()
    if row is not None:
        msg = _message_from_row(row)
        return msg, get_lead(msg.lead_id, conn=c)
    if bounce or not inbound.from_email:
        return None, None
    lead_row = c.execute(
        "SELECT l.* FROM leads l JOIN messages m ON m.lead_id = l.id WHERE lower(trim(l.email)) = ? "
        "AND m.direction = 'outbound' AND m.channel = 'email' AND m.sent_at > ? ORDER BY m.sent_at DESC LIMIT 1",
        (normalize_email(inbound.from_email), iso(now - REPLY_LOOKBACK))).fetchone()
    return None, (_lead_from_row(lead_row) if lead_row is not None else None)


NO_REPLY_TEXT = "(The reply had no text of its own.)"


def record_inbound(inbound: mailer.Inbound, key: str, now: datetime | None = None,
                   conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Record what an inbox message means, once (its Message-ID, else `key`): a reply is logged (log_reply stops the
    sequence), an opt-out also suppresses the address and disqualifies the lead, a hard bounce suppresses the address
    and marks the email, auto-replies and soft bounces are only noted. A message about no lead is not stored.

    Returns {"status": "recorded" | "duplicate" | "unmatched", "kind", "lead_id", "message_id"}.
    """
    now = _utc(now or utcnow())
    key = (inbound.message_id or key)[:300]
    with _write_locked(conn) as c:
        if c.execute("SELECT 1 FROM email_events WHERE inbound_key = ?", (key,)).fetchone():
            return {"status": "duplicate", "kind": inbound.kind, "lead_id": None, "message_id": None}
        msg, lead = _match_inbound(c, inbound, now)
        if lead is None:
            return {"status": "unmatched", "kind": inbound.kind, "lead_id": None, "message_id": None}
        message_id = msg.id if msg is not None else None
        if msg is not None and msg.email_state == "unknown":  # something came back for it: it went out
            stamp = iso(_utc(msg.email_claimed_at) if msg.email_claimed_at else now)
            c.execute("UPDATE messages SET status = CASE WHEN status = 'approved' THEN 'sent' ELSE status END, "
                      "sent_at = COALESCE(sent_at, ?), sent_via = 'smtp', email_state = 'sent', send_error = '' "
                      "WHERE id = ?", (stamp, msg.id))
            _email_event(c, lead.company_id, lead.id, msg.id, "resolved", "It went out: something came back for it",
                         now=now)
        kind = inbound.kind
        detail = inbound.subject
        if kind == mailer.BOUNCE:
            if msg is None:
                return {"status": "unmatched", "kind": kind, "lead_id": None, "message_id": None}
            bounced = next((a for a in inbound.failed_recipients
                            if normalize_email(a) == normalize_email(msg.email_to)), msg.email_to)
            detail = f"Bounced: {inbound.status} {inbound.diagnostic}".strip()
            c.execute("UPDATE messages SET email_state = 'bounced', send_error = ? WHERE id = ? "
                      "AND sent_at IS NOT NULL", (detail[:SEND_ERROR_MAX], msg.id))
            _add_suppression(c, bounced or lead.email, "bounce", lead.company_id, lead.id, msg.id, detail, now)
            _tag_lead(c, lead.id, "email-bounced")
        elif kind in (mailer.SOFT_BOUNCE, mailer.POLICY_REJECT):
            detail = f"{inbound.status} {inbound.diagnostic}".strip() or inbound.subject
        elif kind in (mailer.OPT_OUT, mailer.REPLY):
            log_reply(lead.id, inbound.text or NO_REPLY_TEXT, channel="email", generated_by="inbox", conn=c)
            if kind == mailer.OPT_OUT:
                detail = f"Opt-out: {inbound.opt_out}"
                record_opt_out(lead.id, "email reply", detail, message_id=message_id, now=now, event=False,
                               addresses=[inbound.from_email, msg.email_to if msg is not None else ""], conn=c)
        _email_event(c, lead.company_id, lead.id, message_id, kind, detail, inbound_key=key, now=now)
        if kind in ("bounce", "policy_reject"):
            _email_guard(c, lead.company_id, kind, now)
        return {"status": "recorded", "kind": kind, "lead_id": lead.id, "message_id": message_id}


def email_attention(company_id: int, now: datetime | None = None,
                    conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """What needs the user on the Email sending card: emails that may have been sent ("unknown") or failed, and the
    bounces and opt-outs of the last 7 days."""
    now = _utc(now or utcnow())
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM messages WHERE company_id = ? AND direction = 'outbound' AND channel = 'email' "
                         "AND email_state IN ('unknown', 'failed') AND sent_at IS NULL AND status = 'approved' "
                         "ORDER BY email_claimed_at DESC, id DESC LIMIT 50", (company_id,)).fetchall()
        events = list_email_events(company_id, kinds=("bounce", "opt_out"), since=now - timedelta(days=7), limit=20,
                                   conn=c)
    return {"messages": [_message_from_row(r) for r in rows], "events": events}
