"""Orchestration: run collectors, turn raw signals into scored leads, alert on new hot leads, auto-approve drafts."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import config, mailer, repo
from .collectors import CollectContext, RawSignal, get_collectors
from .config import get_settings
from .db import connect
from .models import Company, Lead, LeadIn
from .notify import notify_hot_leads
from .repo import ScanInProgress

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
                                     location=raw.account_location, source=raw.signal.source)
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
                if raw.signal.source == "google_places" and raw.signal.external_id.startswith("gp:"):
                    # Stored with its lead, in the same transaction: a business is "added" only once it is.
                    repo.record_place_ids(company_id, {raw.signal.external_id[3:]: True}, conn=c)
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
    Raises ScanInProgress while another scan of the company runs (in any process).
    """
    config.refresh_saved_settings()  # a key saved in the dashboard by another process counts at once
    company = repo.get_company(company_id)
    collectors = [c for c in get_collectors(sources) if c.enabled_for(company)]
    run_id = repo.start_scan_run(company_id, trigger)
    try:
        return await _scan(company, run_id, collectors, sources, client)
    except BaseException as exc:  # includes cancellation: never leave a run stuck in "running"
        reason = "Interrupted" if isinstance(exc, asyncio.CancelledError) else f"{type(exc).__name__}: {exc}"
        repo.finish_scan_run(run_id, "failed", {"error": reason})
        if isinstance(exc, Exception):
            repo.set_last_scan(company_id)  # the scheduler retries a failed scan after an hour, not every tick
        raise


async def _scan(company: Company, run_id: int, collectors: list, sources: list[str] | None,
                client: httpx.AsyncClient | None) -> dict[str, Any]:
    company_id = company.id
    settings = get_settings()
    since = repo.utcnow() - timedelta(days=company.signals.lookback_days)

    own_client = client is None
    client = client or _new_client()
    per_collector: dict[str, Any] = {}
    all_raw: list[RawSignal] = []
    try:
        async def run_one(collector) -> None:
            ctx = CollectContext(client=client, since=since, settings=settings)
            timeout = getattr(collector, "timeout_seconds", None) or COLLECTOR_TIMEOUT_SECONDS
            try:
                raw = await asyncio.wait_for(collector.collect(company, ctx), timeout)
                all_raw.extend(raw)
                per_collector[collector.name] = {"found": len(raw), "warnings": ctx.warnings,
                                                 **({"counts": dict(ctx.counts)} if ctx.counts else {})}
            except Exception as exc:
                log.warning("collector %s failed: %s", collector.name, exc)
                per_collector[collector.name] = {"found": 0, "error": f"{type(exc).__name__}: {exc}",
                                                 "warnings": ctx.warnings,
                                                 **({"counts": dict(ctx.counts)} if ctx.counts else {})}

        await asyncio.gather(*(run_one(c) for c in collectors))
    finally:
        if own_client:
            await client.aclose()

    ingest_stats = await asyncio.to_thread(ingest, company_id, all_raw)
    repo.set_last_scan(company_id)
    # Every person who is hot and not alerted yet, including leads that turned hot outside a scan.
    alerts = await alert_new_hot_leads(company_id, company)
    auto_approved = await auto_approve_company(company_id)

    stats = {
        "collectors": per_collector,
        "skipped": [c.name for c in get_collectors(sources) if not c.enabled_for(company)],
        "signals_new": ingest_stats.signals_new,
        "signals_duplicate": ingest_stats.signals_duplicate,
        "leads_new": ingest_stats.leads_new,
        "leads_updated": ingest_stats.leads_updated,
        **alerts,
        "auto_approved": auto_approved,
        "errors": ingest_stats.errors[:20],
    }
    status, problem = scan_status(collectors, per_collector)
    if problem:
        stats["error"] = problem
    repo.finish_scan_run(run_id, status, stats)
    stats["run_id"] = run_id
    stats["status"] = status
    return stats


NOTHING_CONFIGURED = ("No signal source is configured for this company: add keywords, subreddits, GitHub repos, "
                      "job boards, news queries, RSS feeds or Google Maps searches to its profile.")
NOTHING_WORKED = ("No source returned anything and every one failed or reported problems (see the warnings): "
                  "check the network, proxy or API limits.")


def _collector_failed(result: dict[str, Any]) -> bool:
    """It raised, or found nothing and warned (collectors turn failed requests into warnings)."""
    return "error" in result or (not result.get("found") and bool(result.get("warnings")))


def scan_status(collectors: list, per_collector: dict[str, Any]) -> tuple[str, str]:
    """('ok' | 'failed' | 'nothing_configured', why it isn't ok)."""
    if not collectors:
        return "nothing_configured", NOTHING_CONFIGURED
    if all(_collector_failed(per_collector.get(c.name, {})) for c in collectors):
        return "failed", NOTHING_WORKED
    return "ok", ""


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


async def alert_new_hot_leads(company_id: int, company: Company | None = None) -> dict[str, Any]:
    """Alert on hot people not alerted yet (and draft for them in auto_draft mode), once per lead.

    Called after every scan and on every scheduler tick, so leads that turn hot through Claude,
    the API, a CSV import or an edit are alerted too.
    """
    company = company or repo.get_company(company_id)
    leads = await asyncio.to_thread(repo.claim_new_hot_leads, company_id)
    notified: list[str] = []
    drafted = 0
    if leads:
        notified = await notify_hot_leads(company, leads)
        if company.outreach.mode == "auto_draft":
            drafted = await asyncio.to_thread(auto_draft, company, leads)
    return {"newly_hot": [lead.id for lead in leads], "notified": notified, "drafted": drafted}


async def alert_active_companies() -> dict[int, dict[str, Any]]:
    """alert_new_hot_leads for every active company; returns the companies that had new hot leads."""
    results = {}
    for company in repo.list_companies():
        if company.status != "active":
            continue
        try:
            result = await alert_new_hot_leads(company.id, company)
        except Exception:
            log.exception("hot-lead alerts failed for company %s", company.id)
            continue
        if result["newly_hot"]:
            results[company.id] = result
    return results


async def auto_approve_company(company_id: int) -> list[int]:
    """repo.auto_approve_due for one company: the drafts it approved. A failure is logged, never raised, so it
    can't fail the scan or the tick that runs it."""
    try:
        return (await asyncio.to_thread(repo.auto_approve_due, company_id))["approved"]
    except Exception:
        log.exception("auto-approve failed for company %s", company_id)
        return []


async def auto_approve_active_companies() -> dict[int, list[int]]:
    """Auto-approve due drafts for every active company that turned it on; returns {company_id: approved ids}
    for the companies where something was approved. One company's error never stops the others."""
    results = {}
    for company in repo.list_companies():
        if company.status != "active" or not company.outreach.auto_approve:
            continue
        approved = await auto_approve_company(company.id)
        if approved:
            results[company.id] = approved
    return results


async def scan_due_companies(trigger: str = "schedule") -> list[dict[str, Any]]:
    results = []
    for company in repo.companies_due_for_scan():
        try:
            results.append({"company_id": company.id, **await run_scan(company.id, trigger=trigger)})
        except ScanInProgress as exc:  # started elsewhere since we looked: that scan does the work
            log.info("scheduled scan skipped: %s", exc)
        except Exception as exc:
            log.exception("scheduled scan failed for company %s", company.id)
            results.append({"company_id": company.id, "status": "failed", "error": str(exc)})
    return results


# --------------------------------------------------------------------------------------
# Email sending from the user's own mailbox (the dashboard process only: scheduler.email_loop and the dashboard's
# buttons). `openberry mcp` never calls these.
# --------------------------------------------------------------------------------------

INBOX_EVERY = timedelta(minutes=5)     # the inbox is checked this often while OpenBerry is open
RECORD_ATTEMPTS = 3                    # writing a sent email to the database, ~10 seconds in all
RECORD_WAIT_SECONDS = 3.0
# Set when a sent email couldn't be recorded in the database: this process sends nothing more until it restarts.
_sending_stopped = ""


def connected_mailbox() -> mailer.Mailbox | None:
    """The mailbox the Mailbox page saved (another process's save counts at once), or None while it isn't set up."""
    config.refresh_saved_settings()
    return mailer.Mailbox.from_settings(get_settings())


def sending_stopped() -> str:
    return _sending_stopped


def _record_sent(job: mailer.EmailJob, address: str, now: datetime | None) -> bool:
    for attempt in range(RECORD_ATTEMPTS):
        try:
            repo.record_email_sent(job.message_id, job.header_id, address, now=now)
            return True
        except Exception:
            log.exception("couldn't record sent email %s (attempt %s)", job.message_id, attempt + 1)
            if attempt < RECORD_ATTEMPTS - 1:
                time.sleep(RECORD_WAIT_SECONDS)
    return False


def send_next_email(now: datetime | None = None) -> dict[str, Any]:
    """Send at most one approved email from the mailbox (repo.claim_next_email decides which, if any).

    Blocking (SMTP): run it in a worker thread. Returns {"status": "sent" | "idle" | "not_connected" | "stopped" |
    an Outcome kind, "message_id", "text"}. Never raises for a mail problem. `now` is for tests: the claim, the
    email's Date header and the record all use it.
    """
    global _sending_stopped
    box = connected_mailbox()
    if box is None:
        return {"status": "not_connected"}
    if _sending_stopped:
        return {"status": "stopped", "text": _sending_stopped}
    claim = secrets.token_hex(16)
    job = repo.claim_next_email(claim, box.from_email, box.from_name, now=now)
    if job is None:
        return {"status": "idle"}
    outcome = mailer.send(job, box, on_data=lambda: repo.mark_email_data(job.message_id, claim, now=now), now=now)
    if outcome.ok:
        if not _record_sent(job, box.address, now):
            # It went out but the database can't say so: send nothing more until a restart. The email stays in
            # "data" and becomes "may have been sent" (never sent again automatically).
            _sending_stopped = ("An email was sent but OpenBerry couldn't record it in its database, so it stopped "
                                "sending. Restart OpenBerry.")
            log.error("email sending stopped: message %s was sent but not recorded", job.message_id)
            return {"status": "stopped", "message_id": job.message_id, "text": _sending_stopped}
        if box.save_sent and (problem := mailer.save_sent_copy(box, outcome.data)):
            log.warning("couldn't save a copy of email %s in the Sent folder: %s", job.message_id, problem)
        log.info("email sent: message %s", job.message_id)
        return {"status": "sent", "message_id": job.message_id}
    repo.record_email_failure(job.message_id, claim, outcome.kind, outcome.text, box.address, now=now)
    log.warning("email %s not sent (%s); the Outreach page says why", job.message_id, outcome.kind)
    log.debug("email %s: %s", job.message_id, outcome.text)  # a mail server's reply can quote the address
    return {"status": outcome.kind, "message_id": job.message_id, "text": outcome.text}


def inbox_due(now: datetime | None = None) -> bool:
    """A mailbox is connected, its login isn't refused (a wrong password is never tried again by itself: providers
    block an account that keeps trying one) and its inbox wasn't tried in the last INBOX_EVERY."""
    address = get_settings().mailbox_address()
    if not address:
        return False
    state = repo.mailbox_state(address)
    if state["auth_failed"]:
        return False
    tried = state["inbox_tried_at"]
    return not tried or datetime.fromisoformat(tried) <= (now or repo.utcnow()) - INBOX_EVERY


RECORD_FAILED = "OpenBerry couldn't save a message from your inbox ({detail}). It reads it again at the next check."


def check_inbox(now: datetime | None = None) -> dict[str, Any]:
    """Read the inbox (and the junk folder) read-only for replies, opt-outs and bounces about OpenBerry's leads.

    Blocking (IMAP): run it in a worker thread. Each message is recorded once (repo.record_inbound); mail that is about
    no lead is never read in full or stored, nor is OpenBerry's own email coming back (a lead with the mailbox's own
    address). A folder's position moves on only once every message in it was recorded: when one can't be (the
    database is busy), the check fails, sending waits, and the next check reads it again. A refused login pauses the
    mailbox (repo.pause_mailbox_login). Returns {"status": "ok" | "failed" | "not_connected", "checked", "recorded",
    "text"}.
    """
    box = connected_mailbox()
    if box is None:
        return {"status": "not_connected"}
    now = now or repo.utcnow()
    positions = repo.imap_positions(box.address)
    previous = repo.mailbox_state(box.address)
    since = datetime.fromisoformat(previous["inbox_checked_at"]).date() - timedelta(days=1) \
        if previous["inbox_checked_at"] else now.date() - timedelta(days=1)
    index = repo.inbound_index(now)
    own = {box.address, box.username.strip().lower(), box.reader[0].strip().lower()} - {""}
    footers = [c.outreach.email_footer for c in repo.list_companies() if c.outreach.email_footer]

    def relevant(headers: Any) -> bool:
        facts = mailer.header_facts(headers)
        if facts["message_id"] in index["ours"] or facts["from_email"] in own:
            return False  # our own email came back (sent to the mailbox itself), or the test email
        return bool(facts["references"] & index["ours"] or facts["from_email"] in index["leads"] or facts["bounce"]
                    or facts["unsubscribe"])

    checked = recorded = 0
    try:
        with mailer.Inbox(box) as inbox:
            folders = inbox.folders()
            for folder in dict.fromkeys(f for f in (folders["inbox"], folders["junk"]) if f):
                found, position = inbox.fetch_new(folder, positions.get(folder), relevant, since=since)
                for key, raw in found:
                    checked += 1
                    try:
                        inbound = mailer.classify_inbound(raw, footers)
                    except Exception:  # one unreadable message never stops the check
                        log.warning("inbox message %s skipped", key, exc_info=True)
                        continue
                    try:  # a message without a Message-ID is known by its content wherever it is read again
                        result = repo.record_inbound(inbound, mailer.content_key(raw), now=now)
                    except Exception as exc:
                        log.exception("inbox message %s couldn't be recorded", key)
                        raise mailer.MailError(mailer.RETRY, RECORD_FAILED.format(
                            detail=mailer.server_text(exc) or type(exc).__name__)) from exc
                    recorded += result["status"] == "recorded"
                positions[folder] = position  # every message of this folder was handled
    except mailer.MailError as exc:
        if exc.kind == mailer.AUTH:
            repo.pause_mailbox_login(box.address, exc.text, now=now)
        repo.save_inbox_check(box.address, False, exc.text, positions=positions, now=now)
        return {"status": "failed", "text": exc.text, "kind": exc.kind}
    repo.save_inbox_check(box.address, True, positions=positions, now=now)
    return {"status": "ok", "checked": checked, "recorded": recorded}


def send_test_email() -> mailer.Outcome:
    """Send a short test email to the mailbox's own address (never anyone else). Not counted toward any limit."""
    box = connected_mailbox()
    if box is None:
        return mailer.Outcome(mailer.CONFIG, "Connect your mailbox first: fill in every field and save.")
    return mailer.send_test(box)


def collector_overview(company: Company) -> list[dict[str, Any]]:
    """Which sources will run for this company and what each one still needs (and paid-API usage, if any)."""
    config.refresh_saved_settings()
    rows = []
    for c in get_collectors():
        row: dict[str, Any] = {
            "name": c.name,
            "label": c.label,
            "signal_types": list(c.signal_types),
            "requires": c.requires,
            "configured": c.is_configured(company),
            "enabled": c.enabled_for(company),
        }
        if (usage := c.usage() if hasattr(c, "usage") else None) is not None:
            row["usage"] = usage
        rows.append(row)
    return rows
