"""Background signal scans started from the dashboard or the JSON API.

At most one scan per company runs at a time. Scans started elsewhere (the scheduler, the
CLI, Claude via MCP) are detected through their 'running' ScanRun row.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from .. import repo, services
from .ui import as_utc

log = logging.getLogger(__name__)

# A 'running' row older than this is a scan that died with its process.
STALE_AFTER = timedelta(minutes=15)

_tasks: dict[int, asyncio.Task[None]] = {}
_results: dict[int, dict[str, Any]] = {}


def task_running(company_id: int) -> bool:
    task = _tasks.get(company_id)
    return task is not None and not task.done()


def status(company_id: int) -> dict[str, Any]:
    """Whether a scan is running now, plus the latest scan run. Reads the database."""
    runs = repo.list_scan_runs(company_id, limit=1)
    last = runs[0] if runs else None
    db_running = bool(last and last.status == "running"
                      and repo.utcnow() - as_utc(last.started_at) < STALE_AFTER)
    return {
        "company_id": company_id,
        "running": task_running(company_id) or db_running,
        "last_run": last.model_dump(mode="json") if last else None,
        "last_result": _results.get(company_id),
    }


async def _run(company_id: int, trigger: str) -> None:
    try:
        stats = await services.run_scan(company_id, trigger=trigger)
        _results[company_id] = {
            "ok": stats.get("status", "ok") != "failed",
            "finished_at": repo.iso(),
            "signals_new": stats.get("signals_new", 0),
            "leads_new": stats.get("leads_new", 0),
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # report on the dashboard instead of crashing the task
        log.exception("dashboard scan failed for company %s", company_id)
        _results[company_id] = {"ok": False, "finished_at": repo.iso(), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if _tasks.get(company_id) is asyncio.current_task():
            _tasks.pop(company_id, None)


def start(company_id: int, trigger: str = "dashboard") -> bool:
    """Start a scan in the background on the running event loop. False if one is already running."""
    if task_running(company_id):
        return False
    _results.pop(company_id, None)
    _tasks[company_id] = asyncio.get_running_loop().create_task(
        _run(company_id, trigger), name=f"openberry-scan-{company_id}")
    return True


async def cancel_all() -> None:
    """Called on shutdown so no scan task outlives the app."""
    tasks = [t for t in _tasks.values() if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()


async def wait_all(timeout: float = 5.0) -> None:
    """Wait for running scans (used by tests and graceful shutdown)."""
    tasks = [t for t in _tasks.values() if not t.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
