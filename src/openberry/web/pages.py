"""Company-level pages: home, registration board, registration wizard, dashboard, signals,
outreach queue, company profile (settings) and help."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from .. import repo, services
from ..config import get_settings
from ..models import SIGNAL_TYPES, Company, Lead, ScanRun
from . import charts, forms, scans
from .auth import LoginRequired, checked_form, public_registration_open, require_login
from .session import COMPANY_KEY, flash, is_logged_in, remember_company
from .ui import LEAD_SOURCES, choice, int_param, page_info, render

router = APIRouter(dependencies=[Depends(require_login)], include_in_schema=False)
public_router = APIRouter(include_in_schema=False)

REACHED_STATUSES = ("contacted", "replied", "meeting", "won", "lost")
ANSWERED_STATUSES = ("replied", "meeting", "won")
OUTREACH_TABS = (
    ("drafts", "Drafts"), ("approved", "Approved"), ("sent", "Sent"), ("replies", "Replies"),
    ("followups", "Follow-ups due"),
)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def leads_by_id(ids: Any) -> dict[int, Lead]:
    out: dict[int, Lead] = {}
    for lead_id in {i for i in ids if i is not None}:
        lead = repo.find_lead(lead_id)
        if lead is not None:
            out[lead_id] = lead
    return out


def claude_prompts(company_id: int) -> list[tuple[str, str]]:
    return [
        ("Daily triage", f"Use the openberry tools: read company {company_id}'s profile, run a signal scan, "
                         "then show me the 10 hottest leads with why they're hot."),
        ("Find decision-makers", f"Find decision-makers for the account leads of company {company_id} and add them."),
        ("Draft connection notes", f"Draft LinkedIn connection notes for all hot leads of company {company_id} "
                                   "that have no message yet."),
        ("Weekly report", f"Write my weekly pipeline report for company {company_id}."),
    ]


def run_summary(run: ScanRun) -> dict[str, Any]:
    stats = run.stats or {}
    collectors = stats.get("collectors") or {}
    errors = [f"{name}: {c['error']}" for name, c in collectors.items() if isinstance(c, dict) and c.get("error")]
    errors += [str(e) for e in stats.get("errors") or []]
    warnings = [f"{name}: {w}" for name, c in collectors.items() if isinstance(c, dict) for w in c.get("warnings") or []]
    duration = None
    if run.finished_at:
        duration = max(0, int((run.finished_at - run.started_at).total_seconds()))
    return {
        "run": run, "signals_new": stats.get("signals_new"), "leads_new": stats.get("leads_new"),
        "errors": errors, "warnings": warnings, "duration": duration, "note": stats.get("note", ""),
        "collectors": sorted(collectors),
    }


def kpi_tiles(company_id: int, stats: dict[str, Any]) -> list[dict[str, Any]]:
    statuses = stats["statuses"]
    reached = sum(statuses[s] for s in REACHED_STATUSES)
    answered = sum(statuses[s] for s in ANSWERED_STATUSES)
    messages = stats["messages"]
    rate = stats["reply_rate"]
    base = f"/c/{company_id}"
    return [
        {"label": "People leads", "value": stats["people"], "href": f"{base}/leads?kind=person",
         "sub": f"{stats['new_leads_7d']:,} added in 7 days" if stats["new_leads_7d"] else "None added this week"},
        {"label": "Hot leads", "value": stats["tiers"]["hot"], "href": f"{base}/leads?tier=hot", "tier": "hot",
         "sub": f"{stats['tiers']['warm']:,} warm"},
        {"label": "New signals (7d)", "value": stats["signals_7d"], "href": f"{base}/signals",
         "sub": f"{stats['signals_total']:,} all time"},
        {"label": "Contacted", "value": reached, "href": f"{base}/leads?status=contacted",
         "sub": f"{messages['sent']:,} message{'' if messages['sent'] == 1 else 's'} sent"},
        {"label": "Reply rate", "value": None if rate is None else f"{rate}%", "href": f"{base}/outreach?tab=replies",
         "sub": f"{answered:,} replied" if reached else "No outreach yet"},
        {"label": "Drafts to review", "value": messages["draft"], "href": f"{base}/outreach",
         "sub": f"{messages['approved']:,} approved, ready to send"},
    ]


def sources_panel(company: Company) -> list[dict[str, Any]]:
    rows = services.collector_overview(company)
    s = company.signals
    claude_inputs = bool(s.influencers or s.competitor_pages or s.events)
    rows.append({
        "name": "claude", "label": "LinkedIn & events (via Claude)",
        "signal_types": ["influencer_engagement", "competitor_engagement", "event", "job_change"],
        "requires": "LinkedIn influencers, competitor pages or events; Claude checks them with a browser/LinkedIn MCP",
        "configured": claude_inputs, "enabled": claude_inputs, "via_claude": True,
    })
    return rows


# --------------------------------------------------------------------------------------
# Home, demo, company switcher, registration board
# --------------------------------------------------------------------------------------

@router.get("/")
def home(request: Request) -> Response:
    companies = repo.list_companies()
    if not companies:
        return render(request, "welcome.html", {"title": "Welcome"})
    remembered = request.session.get(COMPANY_KEY)
    target = next((c for c in companies if c.id == remembered), companies[0])
    return redirect(f"/c/{target.id}")


@router.post("/demo")
def load_demo(request: Request, form: FormData = Depends(checked_form)) -> Response:
    from ..seed import seed_demo

    company_id = seed_demo()
    flash(request, "Demo data loaded. Everything here is fictional; delete the demo company when you're done.")
    return redirect(f"/c/{company_id}")


@router.get("/switch")
def switch_company(company: str = "") -> Response:
    company_id = int_param(company, default=0, low=0)
    return redirect(f"/c/{company_id}" if company_id else "/companies")


@router.get("/companies")
def companies_board(request: Request) -> Response:
    cards = []
    for company in repo.list_companies():
        stats = repo.company_stats(company.id)
        cards.append({"company": company, "leads": stats["leads_total"], "hot": stats["tiers"]["hot"],
                      "signals_7d": stats["signals_7d"]})
    return render(request, "companies.html", {"cards": cards, "active": "companies", "title": "Registration board"})


@router.get("/help")
def help_page(request: Request) -> Response:
    settings = get_settings()
    companies = repo.list_companies()
    return render(request, "help.html", {
        "active": "help", "title": "Connect Claude & API", "db_path": str(settings.db_path.resolve()),
        "api_token_set": bool(settings.api_token), "example_company": companies[0].id if companies else 1,
        "http_mcp": settings.http_mcp_enabled,
    })


# --------------------------------------------------------------------------------------
# Registration wizard
# --------------------------------------------------------------------------------------

def _registration_mode(request: Request) -> bool:
    """True for anonymous public registration; raises LoginRequired when not allowed at all."""
    if is_logged_in(request):
        return False
    if public_registration_open(request):
        return True
    raise LoginRequired(request.url.path)


def _render_register(request: Request, values: dict[str, Any], errors: dict[str, str], public: bool,
                     status_code: int = 200) -> Response:
    return render(request, "register.html", {
        "values": values, "errors": errors, "active": "register", "title": "Register a company",
        "steps": forms.STEPS, "first_step": forms.first_error_step(errors) if errors else "company",
        "public_mode": public, "honeypot": forms.HONEYPOT,
    }, status_code=status_code, public=public)


@public_router.get("/register")
def register_page(request: Request) -> Response:
    public = _registration_mode(request)
    return _render_register(request, forms.default_values(), {}, public)


@public_router.post("/register")
def register_submit(request: Request, form: FormData = Depends(checked_form)) -> Response:
    public = _registration_mode(request)
    if public and form.get(forms.HONEYPOT):
        return redirect("/register/thanks")
    values = forms.values_from_form(form)
    data, errors = forms.build_company(values)
    if data is None:
        return _render_register(request, values, errors, public, status_code=422)
    company = repo.create_company(data)
    if public:
        return redirect("/register/thanks")
    remember_company(request, company.id)
    flash(request, "Registered! Next: run your first scan or connect Claude.")
    return redirect(f"/c/{company.id}")


@public_router.get("/register/thanks")
def register_thanks(request: Request) -> Response:
    public = _registration_mode(request)
    return render(request, "thanks.html", {"title": "Thank you", "public_mode": public}, public=public)


# --------------------------------------------------------------------------------------
# Company dashboard
# --------------------------------------------------------------------------------------

@router.get("/c/{company_id}")
def dashboard(request: Request, company_id: int) -> Response:
    company = repo.get_company(company_id)
    remember_company(request, company_id)
    stats = repo.company_stats(company_id)
    top_leads, _ = repo.list_leads(company_id, kind="person", sort="score", limit=8)
    recent, _ = repo.list_signals(company_id, limit=10)
    runs = repo.list_scan_runs(company_id, limit=6)
    return render(request, "dashboard.html", {
        "company": company, "active": "dashboard", "title": company.name,
        "stats": stats, "kpis": kpi_tiles(company_id, stats),
        "days": charts.day_columns(stats["signals_by_day"]), "types": charts.type_bars(stats["signals_by_type"]),
        "top_leads": top_leads, "recent": recent, "signal_leads": leads_by_id(s.lead_id for s in recent),
        "followups": repo.followups_due(company_id)[:6], "sources": sources_panel(company),
        "runs": [run_summary(r) for r in runs], "scan": scans.status(company_id),
        "prompts": claude_prompts(company_id),
    })


@router.post("/c/{company_id}/scan")
async def scan_now(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    company = await run_in_threadpool(repo.get_company, company_id)
    current = await run_in_threadpool(scans.status, company_id)
    if current["running"] or not scans.start(company_id):
        flash(request, "A scan is already running for this company.", "info")
    else:
        note = "" if company.status == "active" else " (the company is paused, so scheduled scans are off)"
        flash(request, f"Scan started{note}. This page refreshes when it finishes.", "info")
    return redirect(f"/c/{company_id}")


# --------------------------------------------------------------------------------------
# Signals feed
# --------------------------------------------------------------------------------------

@router.get("/c/{company_id}/signals")
def signals_page(request: Request, company_id: int, type: str = "", source: str = "", page: str = "1") -> Response:
    company = repo.get_company(company_id)
    remember_company(request, company_id)
    sig_type, sig_source = choice(type, SIGNAL_TYPES), choice(source, LEAD_SOURCES)
    _, total = repo.list_signals(company_id, type=sig_type, source=sig_source, limit=1)
    pager = page_info(int_param(page), total)
    items, total = repo.list_signals(company_id, type=sig_type, source=sig_source,
                                     limit=pager["size"], offset=pager["offset"])
    return render(request, "signals.html", {
        "company": company, "active": "signals", "title": "Signals", "items": items, "pager": pager,
        "leads": leads_by_id(s.lead_id for s in items), "filters": {"type": sig_type or "", "source": sig_source or ""},
    })


# --------------------------------------------------------------------------------------
# Outreach queue
# --------------------------------------------------------------------------------------

@router.get("/c/{company_id}/outreach")
def outreach_page(request: Request, company_id: int, tab: str = "drafts") -> Response:
    company = repo.get_company(company_id)
    remember_company(request, company_id)
    tab = choice(tab, dict(OUTREACH_TABS)) or "drafts"
    counts = repo.company_stats(company_id)["messages"]
    replies = repo.list_messages(company_id, direction="inbound", limit=200)
    followups = repo.followups_due(company_id)
    tab_counts = {"drafts": counts["draft"], "approved": counts["approved"], "sent": counts["sent"],
                  "replies": len(replies), "followups": len(followups)}
    if tab == "replies":
        messages = replies
    elif tab == "followups":
        messages = []
    else:
        status = {"drafts": "draft", "approved": "approved", "sent": "sent"}[tab]
        messages = repo.list_messages(company_id, status=status, direction="outbound", limit=200)
    return render(request, "outreach.html", {
        "company": company, "active": "outreach", "title": "Outreach", "tab": tab, "tabs": OUTREACH_TABS,
        "tab_counts": tab_counts, "messages": messages, "followups": followups,
        "leads": leads_by_id(m.lead_id for m in messages),
    })


# --------------------------------------------------------------------------------------
# Company profile (settings)
# --------------------------------------------------------------------------------------

def _render_settings(request: Request, company: Company, values: dict[str, Any], errors: dict[str, str],
                     status_code: int = 200) -> Response:
    return render(request, "settings.html", {
        "company": company, "active": "settings", "title": "Company profile", "values": values, "errors": errors,
        "steps": forms.STEPS, "first_step": forms.first_error_step(errors) if errors else "company",
    }, status_code=status_code)


@router.get("/c/{company_id}/settings")
def settings_page(request: Request, company_id: int) -> Response:
    company = repo.get_company(company_id)
    remember_company(request, company_id)
    return _render_settings(request, company, forms.company_to_values(company), {})


@router.post("/c/{company_id}/settings")
def settings_save(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    company = repo.get_company(company_id)
    values = forms.values_from_form(form)
    data, errors = forms.build_company(values, keep=company)
    if data is None:
        flash(request, "Some fields need attention. Nothing was saved.", "error")
        return _render_settings(request, company, values, errors, status_code=422)
    repo.update_company(company_id, data)
    flash(request, "Profile saved. Lead scores were recalculated.")
    return redirect(f"/c/{company_id}/settings")


@router.post("/c/{company_id}/status")
def settings_status(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    company = repo.get_company(company_id)
    new_status = "paused" if company.status == "active" else "active"
    repo.update_company(company_id, {"status": new_status})
    flash(request, "Company paused: scheduled scans are off." if new_status == "paused"
          else "Company activated: scheduled scans are on.", "info")
    return redirect(f"/c/{company_id}/settings")


@router.post("/c/{company_id}/delete")
def company_delete(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    company = repo.get_company(company_id)
    repo.delete_company(company_id)
    if request.session.get(COMPANY_KEY) == company_id:
        request.session.pop(COMPANY_KEY, None)
    flash(request, f"Deleted {company.name} and all of its leads, signals and messages.", "info")
    return redirect("/companies")
