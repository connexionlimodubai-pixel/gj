"""Background loop that runs each active company's signal scan on its own interval, sends hot-lead alerts and
approves drafts whose auto-approve window has passed."""

from __future__ import annotations

import asyncio
import logging

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
