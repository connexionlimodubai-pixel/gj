"""Orchestration: run collectors, turn raw signals into scored leads, alert on new hot leads."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from . import repo
from .collectors import CollectContext, RawSignal, get_collectors
from .config import get_settings
from .db import connect
from .models import Company, Lead, LeadIn
from .notify import notify_hot_leads

log = logging.getLogger(__name__)

COLLECTOR_TIMEOUT_SECONDS = 120


@dataclass
class IngestStats:
    signals_new: int = 0
    signals_duplicate: int = 0
    leads_new: int = 0
    leads_updated: int = 0
    errors: list[str] = field(default_factory=list)
    lead_ids: set[int] = field(default_factory=set)


def ingest(company_id: int, raw_signals: list[RawSignal]) -> IngestStats:
    """Store raw signals: upsert the person/account behind each one, attach the signal, rescore."""
    stats = IngestStats()
    with connect() as c:
        for raw in raw_signals:
            try:
                if raw.lead is not None:
                    lead_in = raw.lead.model_copy(update={"signals": []})
                elif raw.account or raw.account_domain:
                    lead_in = LeadIn(lead_company=raw.account, company_domain=raw.account_domain,
                                     source=raw.signal.source)
                else:
                    lead_in = None
                lead_id = None
                if lead_in is not None:
                    if not lead_in.source or lead_in.source == "manual":
                        lead_in = lead_in.model_copy(update={"source": raw.signal.source})
                    lead, created = repo.upsert_lead(company_id, lead_in, conn=c, rescore=False)
                    lead_id = lead.id
                    if created:
                        stats.leads_new += 1
                    else:
                        stats.leads_updated += 1
                _, sig_created = repo.add_signal(company_id, raw.signal, lead_id=lead_id, conn=c, rescore=False)
                if sig_created:
                    stats.signals_new += 1
                else:
                    stats.signals_duplicate += 1
                if lead_id is not None:
                    stats.lead_ids.add(lead_id)
            except Exception as exc:  # one bad record must not sink the scan
                stats.errors.append(f"{raw.signal.source}: {exc}")
                log.warning("ingest failed for %s: %s", raw.signal.title, exc)
        # Rescore touched leads (and colleagues inheriting account-level intent) once at the end.
        for lead_id in stats.lead_ids:
            repo._rescore_lead_and_dependents(c, lead_id)
    return stats


def _new_client(timeout: float | None = None) -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=timeout or settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )


async def run_scan(company_id: int, *, trigger: str = "manual", sources: list[str] | None = None,
                   client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Run every configured collector for a company and ingest the results.

    Returns a stats dict (also stored on the scan run) with per-collector counts and errors.
    """
    company = repo.get_company(company_id)
    settings = get_settings()
    collectors = [c for c in get_collectors(sources) if c.enabled_for(company)]
    run_id = repo.start_scan_run(company_id, trigger)
    since = repo.utcnow() - timedelta(days=company.signals.lookback_days)
    hot_before = _hot_ids(company_id)

    own_client = client is None
    client = client or _new_client()
    per_collector: dict[str, Any] = {}
    all_raw: list[RawSignal] = []
    try:
        async def run_one(collector) -> None:
            ctx = CollectContext(client=client, since=since, settings=settings)
            try:
                raw = await asyncio.wait_for(collector.collect(company, ctx), COLLECTOR_TIMEOUT_SECONDS)
                all_raw.extend(raw)
                per_collector[collector.name] = {"found": len(raw), "warnings": ctx.warnings}
            except Exception as exc:
                log.warning("collector %s failed: %s", collector.name, exc)
                per_collector[collector.name] = {"found": 0, "error": f"{type(exc).__name__}: {exc}",
                                                 "warnings": ctx.warnings}

        await asyncio.gather(*(run_one(c) for c in collectors))
    finally:
        if own_client:
            await client.aclose()

    ingest_stats = await asyncio.to_thread(ingest, company_id, all_raw)
    repo.set_last_scan(company_id)

    newly_hot = [lead for lead in _hot_leads(company_id) if lead.id not in hot_before]
    notified: list[str] = []
    drafted = 0
    if newly_hot:
        notified = await notify_hot_leads(company, newly_hot)
        if company.outreach.mode == "auto_draft":
            drafted = await asyncio.to_thread(auto_draft, company, newly_hot)

    stats = {
        "collectors": per_collector,
        "skipped": [c.name for c in get_collectors(sources) if not c.enabled_for(company)],
        "signals_new": ingest_stats.signals_new,
        "signals_duplicate": ingest_stats.signals_duplicate,
        "leads_new": ingest_stats.leads_new,
        "leads_updated": ingest_stats.leads_updated,
        "newly_hot": [lead.id for lead in newly_hot],
        "notified": notified,
        "drafted": drafted,
        "errors": ingest_stats.errors[:20],
    }
    failed = collectors and all("error" in per_collector.get(c.name, {}) for c in collectors)
    repo.finish_scan_run(run_id, "failed" if failed else "ok", stats)
    stats["run_id"] = run_id
    stats["status"] = "failed" if failed else "ok"
    return stats


def default_channel(company: Company, lead: Lead) -> str:
    channels = {c.lower() for c in company.outreach.channels}
    if "linkedin" in channels and (lead.linkedin_url or "email" not in channels):
        return "linkedin_connect"
    if "email" in channels and lead.email:
        return "email"
    return "linkedin_connect" if "linkedin" in channels else "email"


def auto_draft(company: Company, leads: list[Lead]) -> int:
    """Create a first-touch template draft for leads that have no outbound message yet. Never sends."""
    from .outreach import draft_template

    count = 0
    for lead in leads:
        if repo.list_messages(company.id, lead_id=lead.id, direction="outbound", limit=1):
            continue
        signals, _ = repo.list_signals(company.id, lead_id=lead.id, include_account=True, limit=20)
        channel = default_channel(company, lead)
        subject, body = draft_template(company, lead, signals, channel)
        repo.create_message(lead.id, body, channel=channel, subject=subject, generated_by="template")
        count += 1
    return count


def _hot_ids(company_id: int) -> set[int]:
    return {lead.id for lead in _hot_leads(company_id)}


def _hot_leads(company_id: int) -> list[Lead]:
    leads, _ = repo.list_leads(company_id, tier="hot", kind="person", limit=5000)
    return leads


async def scan_due_companies(trigger: str = "schedule") -> list[dict[str, Any]]:
    results = []
    for company in repo.companies_due_for_scan():
        try:
            results.append({"company_id": company.id, **await run_scan(company.id, trigger=trigger)})
        except Exception as exc:
            log.exception("scheduled scan failed for company %s", company.id)
            results.append({"company_id": company.id, "status": "failed", "error": str(exc)})
    return results


def collector_overview(company: Company) -> list[dict[str, Any]]:
    """Which sources will run for this company and what each one still needs."""
    return [
        {
            "name": c.name,
            "label": c.label,
            "signal_types": list(c.signal_types),
            "requires": c.requires,
            "configured": c.is_configured(company),
            "enabled": c.enabled_for(company),
        }
        for c in get_collectors()
    ]
