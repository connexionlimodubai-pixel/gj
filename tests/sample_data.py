"""Test-only sample data: a chauffeur company with leads, signals and drafts. All names are fictional.

Nothing in the product creates this: tests (and dev scripts) seed it with seed_sample_company().
`legacy_demo=True` writes exactly the rows `openberry demo` wrote in 0.1.x, so the guards and the
cleanup for old demo data (openberry.sample_cleanup) can be tested against them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from openberry import repo
from openberry.models import CompanyIn, LeadIn, SignalIn
from openberry.outreach import draft_template

SAMPLE_NAME = "Sample: Desert Line Chauffeurs"
LEGACY_NAME = "Demo: Desert Line Chauffeurs"
SAMPLE_BRAND = "Desert Line"


def _company(*, legacy_demo: bool, name: str, status: str, brand: str = SAMPLE_BRAND) -> dict[str, Any]:
    return {
        "name": LEGACY_NAME if legacy_demo else name,
        "status": "active" if legacy_demo else status,
        "website": "https://example.com",
        "industry": "Ground transportation / Chauffeur services",
        "location": "Dubai, UAE",
        "company_size": "11-50",
        "description": "Chauffeur-driven executive cars and airport transfers for companies in Dubai and Abu Dhabi.",
        "products": "Corporate accounts, airport meet & greet, roadshow and event transport, VIP transfers",
        "value_proposition": "Gives executive assistants and travel managers one reliable chauffeur partner with "
                             "monthly invoicing and a 24/7 dispatcher.",
        "pain_points": "Last-minute bookings falling through, inconsistent car quality, messy expense reports",
        "competitors": ["Careem Business", "Blacklane", "Uber for Business"],
        "contact_name": "Demo User" if legacy_demo else "Sample User",
        "contact_email": "demo@example.com" if legacy_demo else "sample@example.com",
        "requirements": "50 qualified corporate leads per week in the UAE; focus on finance, consulting and events.",
        "leads_per_week": 50,
        "icp": {
            "job_titles": ["Executive Assistant", "Travel Manager", "Office Manager", "Event Manager",
                           "Head of Operations"],
            "seniorities": ["manager", "head", "director", "c_level"],
            "industries": ["Finance", "Consulting", "Events", "Hospitality", "Real Estate"],
            "company_sizes": ["51-200", "201-1000", "1001-5000", "5000+"],
            "locations": ["UAE"],
            "keywords": ["corporate travel", "roadshow", "events", "VIP", "airport"],
            "exclude_keywords": ["student", "intern"],
        },
        "signals": {
            "keywords": ["corporate travel Dubai", "chauffeur", "airport transfer"],
            "news_queries": ["Dubai office opening", "DIFC raises Series A"],
            "hiring_keywords": ["Executive Assistant", "Travel Manager", "Event Manager"],
            "events": ["GITEX Global", "Arabian Travel Market"],
        },
        "outreach": {
            "sender_name": "Sam",
            "sender_title": "Corporate Accounts",
            "tone": "friendly",
            "call_to_action": "Worth a 10-minute call to set up a corporate account?",
            "signature": f"Sam — {brand} Chauffeurs" + (" (demo)" if legacy_demo else ""),
        },
    }


# (name, title, company, industry, size, location, signals[(type, title, days_ago, strength)])
SAMPLE_LEADS = [
    ("Aisha Rahman", "Executive Assistant to the CEO", "Falcon Ridge Capital", "Finance", "201-1000", "Dubai, UAE",
     [("event", "GITEX Global", 2, 60), ("keyword_mention", "Any reliable chauffeur service for a CEO roadshow in Dubai?", 1, 80)]),
    ("Omar Haddad", "Travel Manager", "Northwind Consulting", "Consulting", "1001-5000", "Abu Dhabi, UAE",
     [("competitor_engagement", "Blacklane vs local chauffeurs for corporate travel", 3, 70)]),
    ("Priya Nair", "Head of Operations", "Bluebay Events", "Events", "51-200", "Dubai, United Arab Emirates",
     [("job_change", "Started as Head of Operations at Bluebay Events", 6, 60), ("event", "Arabian Travel Market", 9, 50)]),
    ("Lucas Moreau", "Office Manager", "Atlas Realty Group", "Real Estate", "201-1000", "Dubai, UAE",
     [("keyword_mention", "Recommendations for airport transfer providers for visiting clients", 4, 60)]),
    ("Fatima Al Zaabi", "Event Manager", "Marina Hospitality", "Hospitality", "1001-5000", "Dubai, UAE",
     [("influencer_engagement", "Commented on a post about VIP guest logistics", 5, 50)]),
    ("Daniel Okafor", "Executive Assistant", "Crescent Ventures", "Finance", "51-200", "Riyadh, Saudi Arabia",
     [("competitor_engagement", "Asked about Careem Business pricing", 12, 50)]),
    ("Sara Lindqvist", "Marketing Intern", "Example Startup", "Software", "1-10", "Dubai, UAE",
     [("keyword_mention", "Uni project on Dubai transport", 2, 40)]),
    ("Hamza Siddiqui", "Chief of Staff", "Gulfstream Advisory", "Consulting", "51-200", "Dubai, UAE", []),
    ("Mei Chen", "Travel & Expense Lead", "Orbit Logistics", "Logistics", "5000+", "Dubai, UAE",
     [("profile_visit", "Viewed the {brand} company page", 1, 60)]),
    ("Yousef Karim", "Founder", "Sandstone Studio", "Design", "11-50", "Sharjah, UAE", []),
]

SAMPLE_ACCOUNTS = [
    ("Falcon Ridge Capital", [("hiring", "Hiring: Executive Assistant (DIFC)", 3, 60), ("funding", "Falcon Ridge Capital closes $40M fund", 8, 70)]),
    ("Northwind Consulting", [("company_news", "Northwind Consulting opens new Dubai office", 5, 60)]),
    ("Harbor Lane Bank", [("hiring", "Hiring: Travel Manager, Middle East", 2, 70)]),
]


def seed_sample_company(*, legacy_demo: bool = False, name: str = SAMPLE_NAME, status: str = "active",
                        brand: str = SAMPLE_BRAND) -> int:
    """Create the sample company with 10 people, 3 accounts, their signals and 2 drafts. Returns its id.

    By default the leads are `source="manual"` with `-sample` LinkedIn slugs, and the scan run is a `cli` one.
    `brand` names the company in its signature and in a lead's signal (scripts/screenshots.py uses its own).
    `legacy_demo=True` writes what 0.1.x's `openberry demo` wrote instead (name, status and brand are then fixed):
    "Demo: ..." name, `source="demo"`, `-demo` slugs, demo@example.com and a finished scan run with trigger "demo".
    """
    brand = SAMPLE_BRAND if legacy_demo else brand
    source = "demo" if legacy_demo else "manual"
    slug_suffix = "-demo" if legacy_demo else "-sample"
    label = "Demo" if legacy_demo else "Sample"
    company = repo.create_company(CompanyIn.model_validate(_company(legacy_demo=legacy_demo, name=name,
                                                                    status=status, brand=brand)))
    now = repo.utcnow()
    for full_name, title, comp, industry, size, location, sigs in SAMPLE_LEADS:
        slug = full_name.lower().replace(" ", "-")
        repo.upsert_lead(company.id, LeadIn(
            full_name=full_name, title=title, lead_company=comp, industry=industry, company_size=size,
            location=location, linkedin_url=f"https://www.linkedin.com/in/{slug}{slug_suffix}", source=source,
            signals=[SignalIn(type=t, title=st.format(brand=brand), source=source, strength=s,
                              occurred_at=now - timedelta(days=d), summary=f"{label} signal for {full_name}")
                     for t, st, d, s in sigs],
        ))
    for comp, sigs in SAMPLE_ACCOUNTS:
        repo.upsert_lead(company.id, LeadIn(
            lead_company=comp, source=source,
            signals=[SignalIn(type=t, title=st, source=source, strength=s, occurred_at=now - timedelta(days=d))
                     for t, st, d, s in sigs],
        ))
    leads, _ = repo.list_leads(company.id, kind="person", limit=3)
    for lead in leads[:2]:
        signals, _ = repo.list_signals(company.id, lead_id=lead.id, include_account=True)
        subject, body = draft_template(company, lead, signals, "linkedin_connect")
        repo.create_message(lead.id, body, channel="linkedin_connect", subject=subject)
    run_id = repo.start_scan_run(company.id, "demo" if legacy_demo else "cli")
    repo.finish_scan_run(run_id, "ok", {"note": f"{label.lower()} data", "signals_new": 0,
                                        "leads_new": len(SAMPLE_LEADS)})
    repo.set_last_scan(company.id)
    return company.id
