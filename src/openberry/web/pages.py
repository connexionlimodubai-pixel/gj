"""Company-level pages: home, registration board, registration wizard, dashboard, signals,
outreach queue, company profile (settings) and help."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from .. import repo, services
from ..config import get_settings
from ..models import SIGNAL_TYPES, Company, CompanyIn, Lead, OutreachConfig, ScanRun
from . import charts, forms, scans
from .auth import LoginRequired, checked_form, public_registration_open, require_login
from .ratelimit import client_key, limits, retry_header
from .session import COMPANY_KEY, flash, is_logged_in, remember_company, safe_next
from .ui import LEAD_SOURCES, as_utc, choice, int_param, page_info, render

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


def agent_prompts(company_id: int) -> list[tuple[str, str]]:
    """What the user pastes into their browser agent. Copied prompts become the user's own words, so
    they name the company by id only: the agent reads each approved message from the send queue."""
    return [
        ("Agents with MCP prompts (Claude)",
         f"Use the openberry tools: run the send_approved_messages prompt for company {company_id}."),
        ("Any other MCP-capable agent",
         f"Use the openberry tools: call get_send_queue for company {company_id} and follow its instructions."),
    ]


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return as_utc(value) if isinstance(value, datetime) else None


AGENT_QUEUE_SHOWN = 50  # the highest daily limit: the queue never holds more than what's left today
AGENT_SKIPPED_SHOWN = 20


def agent_status(company: Company, limit: int = AGENT_QUEUE_SHOWN) -> dict[str, Any]:
    """AI agent sending for the dashboard: the send queue plus the state to show.

    state: off | paused | limit (daily limit reached) | on. The on/off switch is the profile's
    outreach.agent_sending; the pause, the counts and the queued messages come from repo.send_queue,
    which applies every guardrail (only approved LinkedIn messages, never-contact list, replies...).
    """
    queue = repo.send_queue(company.id, limit=limit)
    out = company.outreach
    now = datetime.now(timezone.utc)
    paused_until = _as_datetime(queue.get("paused_until")) or _as_datetime(out.agent_paused_until)
    paused = paused_until is not None and paused_until > now
    enabled = bool(out.agent_sending)
    daily_limit = int(queue.get("daily_limit") or out.agent_daily_limit)
    sent = int(queue.get("sent_last_24h") or 0)
    remaining = queue.get("remaining")
    remaining = max(0, daily_limit - sent) if remaining is None else max(0, int(remaining))
    if not enabled:
        state = "off"
    elif paused:
        state = "paused"
    elif remaining <= 0:
        state = "limit"
    else:
        state = "on"
    items = list(queue.get("items") or []) if enabled and not paused else []
    return {
        "state": state, "enabled": enabled, "paused": paused, "paused_until": paused_until if paused else None,
        "pause_reason": (queue.get("pause_reason") or out.agent_pause_reason or "") if paused else "",
        "daily_limit": daily_limit, "sent_last_24h": sent, "remaining": remaining, "items": items,
        # Every message the agent may send, also those waiting for a free slot (items stop at `remaining`).
        "eligible_total": max(len(items), int(queue.get("eligible_total") or 0)),
        # Approved LinkedIn messages the agent may not send, with why (no LinkedIn URL, lead replied...).
        "skipped": list(queue.get("skipped") or []) if enabled and not paused else [],
        "limit_frees_at": _as_datetime(queue.get("limit_frees_at")),
        # Connection requests: at most 80 in any 7 days, and a note on 5 in any 30 days from a free LinkedIn
        # account (repo.send_queue counts every connection request sent for the company, whoever sent it).
        "linkedin_account": out.linkedin_account,
        "connect_sent_7d": int(queue.get("connect_sent_7d") or 0),
        "weekly_connect_limit": int(queue.get("weekly_connect_limit") or repo.AGENT_WEEKLY_CONNECT_LIMIT),
        "connect_notes_30d": int(queue.get("connect_notes_30d") or 0),
        "monthly_note_limit": queue.get("monthly_note_limit"),
        "connect_blocked_reason": queue.get("connect_blocked_reason") or "",
        "connect_blocked_message": repo.connect_blocked_message(queue) if queue.get("connect_blocked_reason") else "",
        "connect_frees_at": _as_datetime(queue.get("connect_frees_at")),
    }


def run_summary(run: ScanRun) -> dict[str, Any]:
    stats = run.stats or {}
    collectors = stats.get("collectors") or {}
    errors = [str(stats["error"])] if stats.get("error") else []  # why the whole run failed or did nothing
    errors += [f"{name}: {c['error']}" for name, c in collectors.items() if isinstance(c, dict) and c.get("error")]
    errors += [str(e) for e in stats.get("errors") or []]
    warnings = [f"{name}: {w}" for name, c in collectors.items() if isinstance(c, dict)
                for w in c.get("warnings") or []]
    duration = None
    if run.finished_at:
        duration = max(0, int((run.finished_at - run.started_at).total_seconds()))
    return {
        "run": run, "signals_new": stats.get("signals_new"), "leads_new": stats.get("leads_new"),
        "errors": errors, "warnings": warnings, "duration": duration, "note": stats.get("note", ""),
        "collectors": sorted(collectors),
    }


def _new_people(stats: dict[str, Any]) -> str:
    """Sub-text of the People tile: people added this week (new_leads_7d counts accounts too)."""
    new_people = stats["new_people_7d"]
    return f"{new_people:,} added in 7 days" if new_people else "None added this week"


def kpi_tiles(company_id: int, stats: dict[str, Any]) -> list[dict[str, Any]]:
    statuses = stats["statuses"]
    reached = sum(statuses[s] for s in REACHED_STATUSES)
    answered = sum(statuses[s] for s in ANSWERED_STATUSES)
    messages = stats["messages"]
    rate = stats["reply_rate"]
    base = f"/c/{company_id}"
    return [
        {"label": "People leads", "value": stats["people"], "href": f"{base}/leads?kind=person",
         "sub": _new_people(stats)},
        {"label": "Hot leads", "value": stats["tiers"]["hot"], "href": f"{base}/leads?tier=hot", "tier": "hot",
         "sub": f"{stats['tiers']['warm']:,} warm"},
        {"label": "New signals (7d)", "value": stats["signals_7d"], "href": f"{base}/signals",
         "sub": f"{stats['signals_total']:,} all time"},
        # Counts every lead reached (replied, meeting, won... too), so it opens all sent messages
        # rather than leads?status=contacted, which would list fewer leads than the tile shows.
        {"label": "Contacted", "value": reached, "href": f"{base}/outreach?tab=sent",
         "sub": f"{messages['sent']:,} message{'' if messages['sent'] == 1 else 's'} sent"},
        {"label": "Reply rate", "value": None if rate is None else f"{rate}%", "href": f"{base}/outreach?tab=replies",
         "sub": f"{answered:,} replied" if reached else "No outreach yet"},
        {"label": "Drafts to review", "value": messages["draft"], "href": f"{base}/outreach",
         "sub": f"{messages['approved']:,} approved to send"},
    ]


# Profile keys some collectors name in `requires`, as the profile form labels them.
FIELD_LABELS = {
    "job_boards": "job boards of target accounts", "rss_feeds": "RSS / Atom feeds", "sec_queries": "SEC EDGAR queries",
    "news_queries": "news queries", "hiring_keywords": "hiring keywords", "github_repos": "GitHub repositories",
}
_FIELD_KEY = re.compile(r"\b(" + "|".join(FIELD_LABELS) + r")\b")


def sources_panel(company: Company) -> list[dict[str, Any]]:
    rows = services.collector_overview(company)
    settings = get_settings()
    for row in rows:
        row["requires"] = _FIELD_KEY.sub(lambda m: FIELD_LABELS[m.group(1)], row["requires"])
        if row["name"] == "reddit" and not (settings.reddit_client_id and settings.reddit_client_secret):
            row["setup_url"] = "/help#server-sources"  # needs the server admin's API app, not a profile field
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


DOCKER_CONTAINER = "openberry"  # container_name in docker-compose.yml
DOCKER_MCP = ["docker", "exec", "-i", DOCKER_CONTAINER, "openberry", "mcp"]
_SHELL_SAFE = re.compile(r"[\w@%+=:,./-]+")


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def source_checkout() -> Path | None:
    """The repository folder when OpenBerry runs from a clone (the README's `uv run` install)."""
    root = Path(__file__).resolve().parents[3]  # <root>/src/openberry/web/pages.py
    return root if (root / "pyproject.toml").is_file() and (root / "src" / "openberry").is_dir() else None


CLI_EXECUTABLE = "openberry-cli"  # the packaged app's console executable


def bundled_cli() -> str:
    """The packaged app's command-line executable: `openberry-cli` (.exe on Windows) next to the
    app's own executable (on macOS both sit in OpenBerry.app/Contents/MacOS).

    Without it, the app's executable, which also runs CLI commands (desktop.main).
    """
    app = Path(sys.executable)
    cli = app.with_name(CLI_EXECUTABLE + (".exe" if sys.platform == "win32" else ""))
    return str(cli if cli.is_file() else app)


MOVE_TO_APPLICATIONS = ("macOS is running OpenBerry from a temporary copy, so Claude would not find it later. "
                        "Quit OpenBerry, drag it into your Applications folder, open it from there, "
                        "then come back to this page.")
EXTRACT_THE_ZIP = ("OpenBerry is running from a temporary folder, probably from inside the zip file, so Claude "
                   "would not find it later. Close OpenBerry, extract the zip file (right-click it, Extract All), "
                   "open OpenBerry from the extracted folder, then come back to this page.")


def _temp_dir() -> str:
    return tempfile.gettempdir()


def unstable_location(command: str) -> str:
    """Why the packaged app's command would not work for Claude later, or "" if its place is fine.

    A downloaded Mac app opened where it was unzipped runs from a random, temporary copy (App
    Translocation), and Windows runs an app opened inside a zip file from a temporary folder: both
    folders disappear, and Claude Desktop's config would point at nothing.
    """
    if "/AppTranslocation/" in command:
        return MOVE_TO_APPLICATIONS
    temp = os.path.normcase(os.path.realpath(_temp_dir()))
    if os.path.normcase(os.path.realpath(command)).startswith(temp.rstrip(os.sep) + os.sep):
        return EXTRACT_THE_ZIP
    return ""


def shell_line(words: list[str]) -> str:
    return " ".join(w if _SHELL_SAFE.fullmatch(w) else '"' + w.replace('"', '\\"') + '"' for w in words)


def mcp_launch(db_path: str) -> dict[str, Any]:
    """How Claude starts `openberry mcp` on this machine: command, args and env.

    Claude Desktop starts servers without the shell's PATH, so the command is an absolute path:
    the packaged desktop app's own CLI, uv for a clone (as in the README), else this Python.
    In a container, `docker exec` runs it there.
    """
    if getattr(sys, "frozen", False):  # the packaged desktop app (PyInstaller): there is no Python to run
        return {"command": bundled_cli(), "args": ["mcp"], "env": {"OPENBERRY_DB": db_path}, "docker": False}
    if in_container():
        return {"command": DOCKER_MCP[0], "args": DOCKER_MCP[1:], "env": {}, "docker": True}
    root, uv = source_checkout(), shutil.which("uv")
    if root is not None and uv:
        command, args = uv, ["--directory", str(root), "run", "openberry", "mcp"]
    else:
        command, args = sys.executable, ["-m", "openberry", "mcp"]
    return {"command": command, "args": args, "env": {"OPENBERRY_DB": db_path}, "docker": False}


@router.get("/help")
def help_page(request: Request) -> Response:
    settings = get_settings()
    companies = repo.list_companies()
    launch = mcp_launch(str(settings.db_path.resolve()))
    desktop = {"mcpServers": {"openberry": {"command": launch["command"], "args": launch["args"],
                                            **({"env": launch["env"]} if launch["env"] else {})}}}
    # Commands use the address this page was opened on: OPENBERRY_BASE_URL may still be the
    # default :8000 while the server runs on another port.
    page_url = str(request.base_url).rstrip("/")
    packaged = bool(getattr(sys, "frozen", False))
    return render(request, "help.html", {
        "active": "help", "title": "Connect Claude & API", "db_path": str(settings.db_path.resolve()),
        "api_token_set": bool(settings.api_token), "example_company": companies[0].id if companies else 1,
        "http_mcp": settings.http_mcp_enabled, "launch": launch, "launch_shell": shell_line(
            [launch["command"], *launch["args"]]), "docker_shell": shell_line(DOCKER_MCP),
        "desktop_config": json.dumps(desktop, indent=2, ensure_ascii=False),
        "page_url": page_url, "base_url_differs": page_url != settings.base_url.rstrip("/"),
        "reddit_ready": bool(settings.reddit_client_id and settings.reddit_client_secret),
        "packaged": packaged, "install_warning": unstable_location(launch["command"]) if packaged else "",
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
                     status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return render(request, "register.html", {
        "values": values, "errors": errors, "active": "register", "title": "Register a company",
        "steps": forms.STEPS, "first_step": forms.first_error_step(errors) if errors else "company",
        "error_steps": forms.error_steps(errors), "public_mode": public, "honeypot": forms.HONEYPOT,
    }, status_code=status_code, public=public, headers=headers)


def _name_taken(name: str) -> bool:
    """Company names identify workspaces on the board and for Claude (mcp_server refuses duplicates too)."""
    wanted = name.strip().casefold()
    return any(c.name.strip().casefold() == wanted for c in repo.list_companies())


def as_pending_review(data: CompanyIn) -> CompanyIn:
    """An anonymous registration waits, paused, until the operator reviews and activates it.

    The scheduler skips paused companies, and the outbound URLs a visitor could choose (RSS feeds,
    alert webhooks) are dropped: the operator adds them while reviewing the profile. The public
    form doesn't show those fields.
    """
    return data.model_copy(update={
        "status": "paused",
        "signals": data.signals.model_copy(update={"rss_feeds": []}),
        "notify": data.notify.model_copy(update={"slack_webhook_url": "", "discord_webhook_url": ""}),
        # Only the operator turns on AI agent sending and auto-approve (the public form shows neither).
        "outreach": data.outreach.model_copy(update={
            "agent_sending": False, "agent_daily_limit": OutreachConfig.model_fields["agent_daily_limit"].default,
            "auto_approve": False, "auto_approve_hours": OutreachConfig.model_fields["auto_approve_hours"].default,
            "auto_approve_since": None}),
    })


@public_router.get("/register")
def register_page(request: Request) -> Response:
    public = _registration_mode(request)
    return _render_register(request, forms.default_values(), {}, public)


@public_router.post("/register")
def register_submit(request: Request, form: FormData = Depends(checked_form)) -> Response:
    public = _registration_mode(request)
    values = forms.values_from_form(form)
    if public and (wait := limits(request).register.allow(client_key(request))):
        flash(request, "Too many registrations from your network. Wait a minute, then submit again.", "error")
        return _render_register(request, values, {}, public, status_code=429, headers=retry_header(wait))
    if public and form.get(forms.HONEYPOT):
        return redirect("/register/thanks")
    data, errors = forms.build_company(values)
    if data is not None and _name_taken(data.name):
        data, errors = None, {"name": "A company with this name is already registered. Use a more specific name, "
                                      "e.g. with your city or country."}
    if data is None:
        return _render_register(request, values, errors, public, status_code=422)
    if public:
        repo.create_company(as_pending_review(data))
        return redirect("/register/thanks")
    company = repo.create_company(data)
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
        "prompts": claude_prompts(company_id), "agent": agent_status(company),
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

def auto_approve_status(company: Company, due: dict[str, Any]) -> dict[str, Any]:
    """Auto-approve for the Outreach page's card, from repo.auto_approve_due's result (run as the page opened)."""
    out = company.outreach
    return {
        "enabled": out.auto_approve, "hours": out.auto_approve_hours, "paused": company.status != "active",
        "waiting": due["waiting"], "held": due["held"], "blocked": due["blocked"],
        "next_at": _as_datetime(due["next_at"]),
    }


@router.get("/c/{company_id}/outreach")
def outreach_page(request: Request, company_id: int, tab: str = "drafts") -> Response:
    company = repo.get_company(company_id)
    remember_company(request, company_id)
    # Approve what is due first, so the page is current even with the scheduler off (OPENBERRY_SCHEDULER=false).
    due = repo.auto_approve_due(company_id)
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
    agent = agent_status(company)
    return render(request, "outreach.html", {
        "company": company, "active": "outreach", "title": "Outreach", "tab": tab, "tabs": OUTREACH_TABS,
        "tab_counts": tab_counts, "messages": messages, "followups": followups,
        "leads": leads_by_id(m.lead_id for m in messages),
        "agent": agent, "agent_prompts": agent_prompts(company_id),
        "agent_queued_ids": {item["message_id"] for item in agent["items"]},
        "agent_leads": leads_by_id(s["lead_id"] for s in agent["skipped"][:AGENT_SKIPPED_SHOWN]),
        "agent_skipped_shown": AGENT_SKIPPED_SHOWN,
        "auto": auto_approve_status(company, due),
        "auto_states": repo.auto_approve_states(company, messages) if company.outreach.auto_approve else {},
    })


def _auto_hours(raw: Any) -> int | None:
    """The submitted review window, or None when it isn't a whole number of hours in the allowed range."""
    text = raw.strip() if isinstance(raw, str) else ""
    low, high = forms.AUTO_APPROVE_HOURS_RANGE
    return int(text) if text.isdigit() and low <= int(text) <= high else None


def hours_text(hours: int) -> str:
    return f"{hours} hour{'' if hours == 1 else 's'}"


@router.post("/c/{company_id}/outreach/auto-approve")
def auto_approve_settings(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    """Turn auto-approve on or off and set its review window. Turning it off always works.

    Turning it on starts every draft's window again from now (auto_approve_since): drafts already waiting are
    never approved at once.
    """
    company = repo.get_company(company_id)
    back = f"/c/{company_id}/outreach#auto-approve"
    turn_on = str(form.get("auto_approve") or "").strip().lower() in forms.TRUTHY
    raw_hours = form.get("auto_approve_hours")
    hours = _auto_hours(raw_hours)
    if not turn_on:
        patch: dict[str, Any] = {"auto_approve": False}
        if hours is not None:
            patch["auto_approve_hours"] = hours
        repo.update_company(company_id, {"outreach": patch})
        flash(request, "Auto-approve is off. Drafts wait for you to approve them.", "info")
        return redirect(back)
    if hours is None and isinstance(raw_hours, str) and raw_hours.strip():
        low, high = forms.AUTO_APPROVE_HOURS_RANGE
        flash(request, f"The review window must be a whole number of hours from {low} to {high}. Nothing was "
                       "changed.", "error")
        return redirect(back)
    hours = hours or company.outreach.auto_approve_hours
    was_on = company.outreach.auto_approve
    patch = {"auto_approve": True, "auto_approve_hours": hours}
    if not was_on:
        patch["auto_approve_since"] = repo.iso()
    repo.update_company(company_id, {"outreach": patch})
    paused = "" if company.status == "active" else " The company is paused: nothing is approved until you activate it."
    if was_on:
        flash(request, f"Review window saved: drafts are approved {hours_text(hours)} after they're written or "
                       f"last edited.{paused}")
    else:
        agent = " With AI agent sending on, your agent may then send the LinkedIn ones." if (
            company.outreach.agent_sending) else ""
        flash(request, f"Auto-approve is on: drafts you don't edit, hold or skip are approved {hours_text(hours)} "
                       f"after they're written. Drafts you already have get {hours_text(hours)} from now.{agent}"
                       f"{paused}")
    return redirect(back)


def _agent_limit(raw: Any) -> int | None:
    """The submitted daily limit, or None when it isn't a whole number in the allowed range."""
    text = raw.strip() if isinstance(raw, str) else ""
    low, high = forms.AGENT_LIMIT_RANGE
    return int(text) if text.isdigit() and low <= int(text) <= high else None


@router.post("/c/{company_id}/outreach/agent")
def agent_settings(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    """Turn AI agent sending on or off and set its daily limit. Turning it off always works."""
    company = repo.get_company(company_id)
    back = f"/c/{company_id}/outreach#agent"
    turn_on = str(form.get("agent_sending") or "").strip().lower() in forms.TRUTHY
    raw_limit = form.get("agent_daily_limit")
    limit = _agent_limit(raw_limit)
    if not turn_on:
        patch: dict[str, Any] = {"agent_sending": False}
        if limit is not None:
            patch["agent_daily_limit"] = limit
        repo.update_company(company_id, {"outreach": patch})
        flash(request, "AI agent sending is off. Approved messages wait for you to send them yourself.", "info")
        return redirect(back)
    if limit is None and isinstance(raw_limit, str) and raw_limit.strip():
        low, high = forms.AGENT_LIMIT_RANGE
        flash(request, f"The daily limit must be a whole number from {low} to {high}. Nothing was changed.", "error")
        return redirect(back)
    limit = limit or company.outreach.agent_daily_limit
    was_on = company.outreach.agent_sending
    company = repo.update_company(company_id, {"outreach": {"agent_sending": True, "agent_daily_limit": limit}})
    paused_until = _as_datetime(company.outreach.agent_paused_until)
    if paused_until is not None and paused_until > datetime.now(timezone.utc):
        flash(request, f"Saved, but agent sending is paused until {paused_until:%d %b %H:%M} UTC because your agent "
                       "reported a problem. Check LinkedIn yourself, then resume below.", "warning")
    elif was_on:
        flash(request, f"Daily limit saved: at most {limit} LinkedIn messages in any 24 hours.")
    else:
        flash(request, f"AI agent sending is on: your agent may send up to {limit} approved LinkedIn messages in "
                       "any 24 hours. Copy the prompt below into your agent.")
    return redirect(back)


@router.post("/c/{company_id}/outreach/agent/resume")
def agent_resume(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    """Lift the pause the agent's problem report set (report_send_problem)."""
    company = repo.get_company(company_id)
    repo.resume_agent_sending(company_id)
    if company.outreach.agent_sending:
        flash(request, "Agent sending resumed. Your agent stops and pauses it again if LinkedIn shows anything "
                       "unexpected.")
    else:
        flash(request, "Pause lifted. Agent sending is still off: turn it on when you want your agent to send.", "info")
    return redirect(safe_next(form.get("next"), f"/c/{company_id}/outreach#agent"))


# --------------------------------------------------------------------------------------
# Company profile (settings)
# --------------------------------------------------------------------------------------

def _render_settings(request: Request, company: Company, values: dict[str, Any], errors: dict[str, str],
                     status_code: int = 200) -> Response:
    return render(request, "settings.html", {
        "company": company, "active": "settings", "title": "Company profile", "values": values, "errors": errors,
        "steps": forms.STEPS, "first_step": forms.first_error_step(errors) if errors else "company",
        "error_steps": forms.error_steps(errors),
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
    """Pause or activate; toggles unless the form says which (the board's Activate button does)."""
    company = repo.get_company(company_id)
    wanted = form.get("status")
    new_status = wanted if wanted in ("active", "paused") else ("paused" if company.status == "active" else "active")
    repo.update_company(company_id, {"status": new_status})
    flash(request, f"{company.name} paused: scheduled scans are off." if new_status == "paused"
          else f"{company.name} activated: scheduled scans are on.", "info")
    return redirect(safe_next(form.get("next"), f"/c/{company_id}/settings"))


@router.post("/c/{company_id}/delete")
def company_delete(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    company = repo.get_company(company_id)
    repo.delete_company(company_id)
    if request.session.get(COMPANY_KEY) == company_id:
        request.session.pop(COMPANY_KEY, None)
    flash(request, f"Deleted {company.name} and all of its leads, signals and messages.", "info")
    return redirect("/companies")
