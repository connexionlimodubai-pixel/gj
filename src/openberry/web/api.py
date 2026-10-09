"""JSON API for scripts and automation tools (n8n, Activepieces, Zapier-style webhooks).

Authentication: `Authorization: Bearer $OPENBERRY_API_TOKEN` (see auth.require_api_auth).
Errors: 404 {"detail"} for unknown ids, 422 {"detail"} for invalid input.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, Response

from .. import repo, website
from ..models import CompanyIn, LeadIn, OutreachConfig
from . import scans
from .auth import is_anonymous, require_api_auth
from .ratelimit import client_key, limits, retry_header

router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_api_auth)])


class SiteSummaryIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


# --------------------------------------------------------------------------------------
# Companies
# --------------------------------------------------------------------------------------

# AI agent sending is the user's decision, made in the dashboard (as with Claude's update_company): a
# script may turn it off or lower the daily limit, never turn it on, raise the limit or lift the pause
# the agent set with report_send_problem.
AGENT_SETTINGS = ("agent_sending", "agent_daily_limit", "agent_paused_until", "agent_pause_reason")


def _check_agent_settings(changes: Any, current: OutreachConfig) -> None:
    if not isinstance(changes, dict) or not any(key in changes for key in AGENT_SETTINGS):
        return
    wanted = OutreachConfig.model_validate(
        {**current.model_dump(mode="json"), **{k: v for k, v in changes.items() if k in AGENT_SETTINGS}})
    refused = []
    if wanted.agent_sending and not current.agent_sending:
        refused.append("turn AI agent sending on")
    if wanted.agent_daily_limit > current.agent_daily_limit:
        refused.append("raise the agent's daily limit")
    if (wanted.agent_paused_until != current.agent_paused_until
            or wanted.agent_pause_reason != current.agent_pause_reason):
        refused.append("pause or resume agent sending")
    if refused:
        raise HTTPException(403, f"Not changed: only the user can {' or '.join(refused)}, on the dashboard's "
                                 "Outreach page. The API may turn agent sending off or lower its daily limit.")


@router.get("/companies")
def api_list_companies() -> dict[str, Any]:
    return {"items": [_dump(c) for c in repo.list_companies()]}


@router.post("/companies", status_code=201)
def api_create_company(data: CompanyIn) -> dict[str, Any]:
    _check_agent_settings(data.outreach.model_dump(mode="json", include=set(AGENT_SETTINGS)), OutreachConfig())
    return _dump(repo.create_company(data))


@router.get("/companies/{company_id}")
def api_get_company(company_id: int) -> dict[str, Any]:
    return _dump(repo.get_company(company_id))


@router.patch("/companies/{company_id}")
def api_patch_company(company_id: int, patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Deep-merge a partial profile, e.g. {"icp": {"locations": ["UAE"]}} or {"status": "paused"}."""
    _check_agent_settings(patch.get("outreach"), repo.get_company(company_id).outreach)
    return _dump(repo.update_company(company_id, patch))


@router.get("/companies/{company_id}/stats")
def api_company_stats(company_id: int) -> dict[str, Any]:
    repo.get_company(company_id)
    return repo.company_stats(company_id)


# --------------------------------------------------------------------------------------
# Leads
# --------------------------------------------------------------------------------------

@router.get("/companies/{company_id}/leads")
def api_list_leads(company_id: int, tier: str | None = None, status: str | None = None,
                   min_score: int | None = Query(None, ge=0, le=100), search: str | None = None,
                   kind: str | None = None, source: str | None = None, sort: str = "score",
                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    repo.get_company(company_id)
    items, total = repo.list_leads(company_id, tier=tier, status=status, min_score=min_score, search=search,
                                   kind=kind, source=source, sort=sort, limit=limit, offset=offset)
    return {"total": total, "items": [_dump(lead) for lead in items]}


@router.post("/companies/{company_id}/leads")
def api_upsert_lead(company_id: int, data: LeadIn, response: Response) -> dict[str, Any]:
    """Add a lead, or merge it into the existing one with the same identity (201 created / 200 merged)."""
    lead, created = repo.upsert_lead(company_id, data)
    response.status_code = 201 if created else 200
    return {"created": created, "lead": _dump(lead)}


@router.get("/leads/{lead_id}")
def api_get_lead(lead_id: int) -> dict[str, Any]:
    lead = repo.get_lead(lead_id)
    signals, _ = repo.list_signals(lead.company_id, lead_id=lead_id, include_account=True, limit=100)
    messages = repo.list_messages(lead.company_id, lead_id=lead_id, limit=200)
    return {**_dump(lead), "signals": [_dump(s) for s in signals], "messages": [_dump(m) for m in messages]}


@router.patch("/leads/{lead_id}")
def api_patch_lead(lead_id: int, fields: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Update status, notes, tags or profile fields, e.g. {"status": "qualified"}."""
    return _dump(repo.update_lead(lead_id, fields))


# --------------------------------------------------------------------------------------
# Signals and scans
# --------------------------------------------------------------------------------------

@router.get("/companies/{company_id}/signals")
def api_list_signals(company_id: int, type: str | None = None, source: str | None = None,
                     lead_id: int | None = None, since: datetime | None = None,
                     limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    repo.get_company(company_id)
    items, total = repo.list_signals(company_id, type=type, source=source, lead_id=lead_id, since=since,
                                     limit=limit, offset=offset)
    return {"total": total, "items": [_dump(s) for s in items]}


@router.post("/companies/{company_id}/scan", status_code=202)
async def api_start_scan(company_id: int) -> Any:
    await run_in_threadpool(repo.get_company, company_id)
    current = await run_in_threadpool(scans.status, company_id)
    if current["running"] or not scans.start(company_id, trigger="api"):
        return JSONResponse({"started": False, "detail": "A scan is already running for this company."},
                            status_code=409)
    return {"started": True, "status_url": f"/api/companies/{company_id}/scan-status"}


@router.get("/companies/{company_id}/scan-status")
def api_scan_status(company_id: int) -> dict[str, Any]:
    repo.get_company(company_id)
    return scans.status(company_id)


# --------------------------------------------------------------------------------------
# Website auto-fill (also used by the registration form)
# --------------------------------------------------------------------------------------

@router.post("/site-summary")
async def api_site_summary(body: SiteSummaryIn, request: Request) -> dict[str, Any]:
    if is_anonymous(request) and (wait := limits(request).site_summary.allow(client_key(request))):
        raise HTTPException(429, "Too many website look-ups. Wait a minute and try again, or fill in the form by hand.",
                            headers=retry_header(wait))
    try:
        summary = await website.fetch_site_summary(body.url)
    except website.UnsafeURL as exc:
        raise HTTPException(422, f"Can't read that address: {exc}.") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"The website answered with HTTP {exc.response.status_code}.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach the website ({type(exc).__name__}).") from exc
    return {"summary": summary, "suggestions": website.suggest_profile(summary)}
