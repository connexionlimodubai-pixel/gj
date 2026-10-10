"""Background loops of the dashboard process: one runs each active company's signal scan on its own interval, sends
hot-lead alerts and approves drafts whose auto-approve window has passed; the other sends approved emails from the
user's mailbox and reads its inbox (email_loop)."""

from __future__ import annotations

import asyncio
import logging

from . import repo, services
from .config import get_settings
from .services import alert_active_companies, auto_approve_active_companies, scan_due_companies

log = logging.getLogger(__name__)


async def scheduler_loop(stop: asyncio.Event | None = None) -> None:
    """Every tick, scan companies whose last scan is older than their scan_interval_hours.

    Then alert on people who turned hot since the last tick outside a scan (Claude, API, CSV, edits), and
    approve the drafts whose review window has passed in companies that turned auto-approve on.
    """
    stop = stop or asyncio.Event()
    tick = max(30, get_settings().scheduler_tick_seconds)
    log.info("scheduler started (tick %ss)", tick)
    while not stop.is_set():
        try:
            results = await scan_due_companies()
            for r in results:
                log.info("scheduled scan company=%s status=%s new_signals=%s", r.get("company_id"),
                         r.get("status"), r.get("signals_new"))
        except Exception:  # keep the loop alive whatever happens
            log.exception("scheduler tick failed")
        try:
            for company_id, r in (await alert_active_companies()).items():
                log.info("hot-lead alert company=%s leads=%s notified=%s drafted=%s", company_id,
                         r["newly_hot"], r["notified"], r["drafted"])
        except Exception:
            log.exception("hot-lead alerts failed")
        try:
            for company_id, approved in (await auto_approve_active_companies()).items():
                log.info("auto-approved company=%s messages=%s", company_id, approved)
        except Exception:
            log.exception("auto-approve failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass


EMAIL_TICK_SECONDS = 60


async def email_loop(stop: asyncio.Event, wake: asyncio.Event | None = None, tick: float = EMAIL_TICK_SECONDS) -> None:
    """Email from the user's mailbox, in its own task so a slow mail server never holds up scans or alerts.

    Every tick (or at once when `wake` is set: the dashboard's Send now), while a mailbox is connected: expire stale
    claims, check the inbox every few minutes (replies, opt-outs, bounces), then send at most one approved email
    (repo.claim_next_email applies every rule: the limits, the minutes between two emails, the sending hours...).
    Only the dashboard process runs it (openberry serve, the desktop app), never `openberry mcp`.
    """
    wake = wake or asyncio.Event()
    log.info("email loop started (tick %ss)", tick)
    while not stop.is_set():
        wake.clear()
        try:
            await asyncio.to_thread(repo.expire_email_claims)
            if await asyncio.to_thread(services.inbox_due):
                result = await asyncio.to_thread(services.check_inbox)
                if result.get("status") == "failed":
                    log.warning("inbox check failed: %s", result.get("text"))
            result = await asyncio.to_thread(services.send_next_email)
            if result["status"] not in ("idle", "not_connected"):
                log.info("email loop: %s", {k: v for k, v in result.items() if k != "text"})
        except Exception:  # keep the loop alive whatever happens
            log.exception("email loop tick failed")
        stopping = asyncio.ensure_future(stop.wait())
        woken = asyncio.ensure_future(wake.wait())
        try:
            await asyncio.wait({stopping, woken}, timeout=tick, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (stopping, woken):
                waiter.cancel()
