"""Background loop that runs each active company's signal scan on its own interval."""

from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .services import scan_due_companies

log = logging.getLogger(__name__)


async def scheduler_loop(stop: asyncio.Event | None = None) -> None:
    """Every tick, scan companies whose last scan is older than their scan_interval_hours."""
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
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
