"""CSV import/export so leads can move to/from LinkedIn exports, Sales Navigator, lemlist, a CRM, etc."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from . import repo
from .db import connect
from .models import Lead, LeadIn

EXPORT_COLUMNS = [
    "id", "full_name", "title", "lead_company", "company_domain", "industry", "company_size", "location",
    "linkedin_url", "email", "phone", "website", "github_username", "twitter", "score", "tier", "icp_score",
    "intent_score", "ai_score", "status", "source", "tags", "last_signal_at", "top_reasons", "notes",
]

# header (lowercased, non-alphanumerics removed) -> LeadIn field
HEADER_ALIASES: dict[str, str] = {
    "name": "full_name", "fullname": "full_name", "contactname": "full_name", "person": "full_name",
    "firstname": "_first", "first": "_first", "givenname": "_first",
    "lastname": "_last", "last": "_last", "surname": "_last", "familyname": "_last",
    "title": "title", "jobtitle": "title", "position": "title", "headline": "title", "role": "title",
    "company": "lead_company", "companyname": "lead_company", "organization": "lead_company",
    "organisation": "lead_company", "account": "lead_company", "employer": "lead_company", "leadcompany": "lead_company",
    "domain": "company_domain", "companydomain": "company_domain", "companywebsite": "company_domain",
    "industry": "industry", "companyindustry": "industry",
    "companysize": "company_size", "employees": "company_size", "headcount": "company_size",
    "numberofemployees": "company_size",
    "location": "location", "city": "location", "country": "location", "region": "location",
    "linkedin": "linkedin_url", "linkedinurl": "linkedin_url", "linkedinprofile": "linkedin_url",
    "profileurl": "linkedin_url", "linkedinprofileurl": "linkedin_url", "url": "profile_url",
    "email": "email", "emailaddress": "email", "workemail": "email",
    "phone": "phone", "phonenumber": "phone", "mobile": "phone",
    "website": "website", "github": "github_username", "githubusername": "github_username",
    "twitter": "twitter", "x": "twitter", "bio": "bio", "summary": "bio", "about": "bio",
    "notes": "notes", "note": "notes", "tags": "tags",
}

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
            if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
                row[key] = "'" + value
        writer.writerow(row)
    return buf.getvalue()


def import_leads_csv(company_id: int, text: str, source: str = "csv") -> dict[str, Any]:
    """Import leads from CSV text. Unknown columns are ignored; duplicates are merged."""
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    mapping = {h: HEADER_ALIASES.get(_norm_header(h)) for h in (reader.fieldnames or [])}
    if not any(mapping.values()):
        raise ValueError("no recognisable columns; include at least 'name' or 'company' "
                         "(also understood: first name, last name, title, linkedin, email, location...)")
    stats = {"created": 0, "merged": 0, "skipped": 0, "errors": []}
    with connect() as c:
        repo.get_company(company_id, conn=c)
        touched: list[int] = []
        for i, row in enumerate(reader, start=2):
            if i - 1 > MAX_IMPORT_ROWS:
                stats["errors"].append(f"stopped after {MAX_IMPORT_ROWS} rows")
                break
            data: dict[str, Any] = {}
            first = last = ""
            for header, field in mapping.items():
                value = (row.get(header) or "").strip()
                if not field or not value:
                    continue
                if field == "_first":
                    first = value
                elif field == "_last":
                    last = value
                elif field == "profile_url" and "linkedin.com/" in value.lower():
                    data.setdefault("linkedin_url", value)
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
                stats["errors"].append(f"row {i}: {exc}")
        for lead_id in touched:
            repo._rescore_lead_and_dependents(c, lead_id)
    stats["errors"] = stats["errors"][:20]
    return stats
