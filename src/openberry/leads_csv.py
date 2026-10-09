"""CSV import/export so leads can move to/from LinkedIn exports, Sales Navigator, lemlist, a CRM, etc."""

from __future__ import annotations

import csv
import io
import itertools
import re
from typing import Any

from . import repo
from .db import connect
from .models import Lead, LeadIn

EXPORT_COLUMNS = [
    "id", "full_name", "title", "lead_company", "company_domain", "industry", "company_size", "location",
    "linkedin_url", "email", "phone", "website", "github_username", "twitter", "profile_url", "bio", "score",
    "tier", "icp_score", "intent_score", "ai_score", "status", "source", "tags", "last_signal_at", "top_reasons",
    "notes",
]

# header (lowercased, non-alphanumerics removed) -> LeadIn field
HEADER_ALIASES: dict[str, str] = {
    "name": "full_name", "fullname": "full_name", "contactname": "full_name", "person": "full_name",
    "firstname": "_first", "first": "_first", "givenname": "_first",
    "lastname": "_last", "last": "_last", "surname": "_last", "familyname": "_last",
    "title": "title", "jobtitle": "title", "position": "title", "headline": "title", "role": "title",
    "company": "lead_company", "companyname": "lead_company", "organization": "lead_company",
    "organisation": "lead_company", "account": "lead_company", "employer": "lead_company", "leadcompany": "lead_company",
    "companynameforemails": "lead_company",
    "domain": "company_domain", "companydomain": "company_domain", "companywebsite": "company_domain",
    "industry": "industry", "companyindustry": "industry",
    "companysize": "company_size", "employees": "company_size", "headcount": "company_size",
    "numberofemployees": "company_size",
    "location": "location", "city": "location", "country": "location", "region": "location",
    "linkedin": "linkedin_url", "linkedinurl": "linkedin_url", "linkedinprofile": "linkedin_url",
    "linkedinprofileurl": "linkedin_url", "personlinkedinurl": "linkedin_url",
    "profileurl": "profile_url", "url": "profile_url",
    "email": "email", "emailaddress": "email", "workemail": "email",
    "phone": "phone", "phonenumber": "phone", "mobile": "phone", "mobilephone": "phone", "firstphone": "phone",
    "workdirectphone": "phone", "directphone": "phone", "corporatephone": "phone", "workphone": "phone",
    "website": "website", "github": "github_username", "githubusername": "github_username",
    "twitter": "twitter", "twitterurl": "twitter", "x": "twitter", "bio": "bio", "summary": "bio", "about": "bio",
    "notes": "notes", "note": "notes", "tags": "tags",
}

# A header row has to name one of these; rows with none of them are skipped.
_IDENTITY_FIELDS = frozenset({"full_name", "_first", "_last", "lead_company"})
# Several columns add up to one value: City, Country -> "Dubai, United Arab Emirates".
_JOINED_FIELDS = frozenset({"location"})
_DELIMITERS = (",", ";", "\t")
_HEADER_SCAN_LINES = 20  # LinkedIn's Connections.csv has a few "Notes:" lines above its header
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

MAX_IMPORT_ROWS = 5000


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def export_leads_csv(leads: list[Lead]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row: dict[str, Any] = lead.model_dump()
        row["tags"] = ", ".join(lead.tags)
        row["top_reasons"] = " | ".join(lead.score_reasons[:4])
        row["last_signal_at"] = lead.last_signal_at.isoformat() if lead.last_signal_at else ""
        # Neutralise spreadsheet formula injection (=, +, -, @ at the start of a cell).
        for key, value in row.items():
            if isinstance(value, str) and value[:1] in _FORMULA_PREFIXES:
                row[key] = "'" + value
        writer.writerow(row)
    return buf.getvalue()


def _unescape(value: str) -> str:
    """Undo the export's formula escape ("'+971 50..." -> "+971 50...") so an export re-imports as it was."""
    return value[1:] if value[:1] == "'" and value[1:2] in _FORMULA_PREFIXES else value


def _split(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter), [])
    except csv.Error:
        return []


def _locate_header(text: str) -> tuple[int, int, str]:
    """(line number, offset, delimiter) of the header row: the first line naming a person or company column.

    Only the delimiter is guessed. csv.Sniffer also guesses the quote character, and the export's
    "'+ Title matches 'X'" cells make it pick "'", which shifts every column.
    """
    offset = 0
    for number, line in enumerate(itertools.islice(io.StringIO(text, newline=""), _HEADER_SCAN_LINES), start=1):
        cells = {d: [HEADER_ALIASES.get(_norm_header(h)) for h in _split(line, d)] for d in _DELIMITERS}
        delimiter = max(_DELIMITERS, key=lambda d: sum(f is not None for f in cells[d]))
        if _IDENTITY_FIELDS.intersection(cells[delimiter]):
            return number, offset, delimiter
        offset += len(line)
    return 1, 0, ","


def import_leads_csv(company_id: int, text: str, source: str = "csv") -> dict[str, Any]:
    """Import leads from CSV text. Unknown columns are ignored; duplicates are merged.

    Returns {"created", "merged", "skipped", "errors"}; rows without a name or company are skipped.
    """
    text = text.lstrip("﻿")
    header_line, offset, delimiter = _locate_header(text)
    reader = csv.DictReader(io.StringIO(text[offset:], newline=""), delimiter=delimiter)
    mapping = {h: HEADER_ALIASES.get(_norm_header(h)) for h in (reader.fieldnames or [])}
    if not _IDENTITY_FIELDS.intersection(mapping.values()):
        raise ValueError("no recognisable columns; include at least 'name' or 'company' "
                         "(also understood: first name, last name, title, linkedin, email, location...)")
    stats = {"created": 0, "merged": 0, "skipped": 0, "errors": []}
    with connect() as c:
        repo.get_company(company_id, conn=c)
        touched: list[int] = []
        for n, row in enumerate(reader, start=1):
            if n > MAX_IMPORT_ROWS:
                stats["errors"].append(f"stopped after {MAX_IMPORT_ROWS} rows")
                break
            data: dict[str, Any] = {}
            first = last = ""
            for header, field in mapping.items():
                value = _unescape((row.get(header) or "").strip()).strip()
                if not field or not value:
                    continue
                if field == "_first":
                    first = value
                elif field == "_last":
                    last = value
                elif field == "profile_url" and "linkedin.com/" in value.lower():
                    data.setdefault("linkedin_url", value)
                elif field in _JOINED_FIELDS and field in data:
                    if value.casefold() not in data[field].casefold():
                        data[field] += f", {value}"
                else:
                    data.setdefault(field, value)
            if not data.get("full_name") and (first or last):
                data["full_name"] = f"{first} {last}".strip()
            if not data.get("full_name") and not data.get("lead_company"):
                stats["skipped"] += 1
                continue
            try:
                lead, created = repo.upsert_lead(company_id, LeadIn(source=source, **data), conn=c, rescore=False)
                touched.append(lead.id)
                stats["created" if created else "merged"] += 1
            except Exception as exc:
                stats["errors"].append(f"row {header_line + n}: {exc}")
        for lead_id in touched:
            repo._rescore_lead_and_dependents(c, lead_id)
    stats["errors"] = stats["errors"][:20]
    return stats
