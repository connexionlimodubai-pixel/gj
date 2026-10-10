"""API keys page: keys for optional sources, saved to the data folder's .env file (envfile.py).

Logged-in dashboard users only (in local mode: this machine). The JSON API and the MCP server never read or set
these settings, and a saved key is never shown again: the page says "ending in 1234" at most.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from .. import config, envfile
from ..collectors import google_places
from .auth import checked_form, require_login
from .session import flash
from .ui import render

router = APIRouter(dependencies=[Depends(require_login)], include_in_schema=False)

GOOGLE_KEY = "OPENBERRY_GOOGLE_PLACES_KEY"
GOOGLE_LIMIT = "OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT"
KEY_RE = re.compile(r"[A-Za-z0-9_-]{20,200}")
GOOGLE_BACK = "/keys#google-maps"


def _back() -> RedirectResponse:
    return RedirectResponse(GOOGLE_BACK, status_code=303)


def keys_context() -> dict[str, Any]:
    places = google_places.usage_summary()  # re-reads settings another process saved
    return {
        "active": "keys", "title": "API keys",
        "env_path": str(config.settings_file()),
        "google": envfile.setting_state(GOOGLE_KEY),
        "google_limit": envfile.setting_state(GOOGLE_LIMIT),
        "places": places,
        "free_per_month": google_places.GOOGLE_FREE_PER_MONTH,
        "resets_label": google_places.day_month(places["resets_on"]),
    }


@router.get("/keys")
def keys_page(request: Request) -> Response:
    return render(request, "keys.html", keys_context())


def _save(request: Request, changes: dict[str, str | None], what: str) -> bool:
    """Save through envfile; flash why not and return False when it can't."""
    try:
        envfile.save_settings(changes)
    except envfile.SettingNotSaved as exc:
        flash(request, str(exc), "error")
        return False
    except OSError as exc:
        flash(request, f"Couldn't save the {what}: {exc.strerror or type(exc).__name__}.", "error")
        return False
    return True


@router.post("/keys/google-maps")
async def google_maps_save(request: Request, form: FormData = Depends(checked_form)) -> Response:
    """action = save_key | remove_key | save_limit. Always redirects to /keys#google-maps with a flash.

    The key never goes into a flash, a log line, an error message or the page.
    """
    action = str(form.get("action") or "")
    if action == "save_key":
        key = str(form.get("key") or "").strip()
        if not KEY_RE.fullmatch(key):
            flash(request, "Paste the whole key: letters, digits, - and _ only.", "error")
            return _back()
        if not await run_in_threadpool(_save, request, {GOOGLE_KEY: key}, "key"):
            return _back()
        ok, why = await google_places.check_key(key)
        if ok:
            flash(request, "Google Maps key saved. Google accepted it.")
        elif ok is False:
            flash(request, f"Google Maps key saved, but Google refused it: {why}", "warning")
        else:
            flash(request, f"Google Maps key saved. OpenBerry couldn't reach Google to check it ({why}).", "info")
    elif action == "remove_key":
        if await run_in_threadpool(_save, request, {GOOGLE_KEY: None}, "key"):
            flash(request, "Google Maps key removed. Google Maps searches are off.", "info")
    elif action == "save_limit":
        raw = str(form.get("limit") or "").strip().replace(",", "")
        high = google_places.GOOGLE_FREE_PER_MONTH
        if not raw.isdigit() or int(raw) > high:
            flash(request, f"Enter a whole number from 0 to {high:,}.", "error")
            return _back()
        limit = int(raw)
        if await run_in_threadpool(_save, request, {GOOGLE_LIMIT: str(limit)}, "limit"):
            flash(request, f"Monthly limit saved: {limit:,} searches." if limit
                  else "Monthly limit saved: 0. Google Maps searches are off.")
    else:
        flash(request, "Nothing was changed.", "info")
    return _back()
