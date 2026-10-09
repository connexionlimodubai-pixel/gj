"""Session helpers: login state, CSRF token, flash messages and safe redirects."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from fastapi import Request

from ..config import get_settings

CSRF_KEY = "csrf"
AUTH_KEY = "auth"
FLASH_KEY = "flashes"
COMPANY_KEY = "company_id"
MAX_FLASHES = 5
# The session lives in a signed cookie and browsers silently drop cookies over 4 KB, which would
# log the user out and break every form's CSRF token. Flashes can quote user data (lead names,
# file names, import errors), so they are bounded per message and in total (JSON-encoded size).
MAX_FLASH_CHARS = 240
MAX_FLASH_BYTES = 1500


def password_fingerprint() -> str:
    """Stored in the session at login, so changing the password logs everyone out."""
    settings = get_settings()
    return hashlib.sha256(f"{settings.secret_key}:{settings.password}".encode()).hexdigest()[:32]


def is_logged_in(request: Request) -> bool:
    """True in local mode (no password) or when this session logged in with the current password."""
    if not get_settings().password:
        return True
    stored = request.session.get(AUTH_KEY)
    return isinstance(stored, str) and hmac.compare_digest(stored, password_fingerprint())


def log_in(request: Request) -> None:
    request.session.clear()
    request.session[AUTH_KEY] = password_fingerprint()
    request.session[CSRF_KEY] = secrets.token_urlsafe(32)


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_KEY] = token
    return token


def csrf_valid(request: Request, token: object) -> bool:
    expected = request.session.get(CSRF_KEY)
    if not isinstance(token, str) or not token or not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


def flash(request: Request, message: str, category: str = "success") -> None:
    """Queue a message for the next rendered page. Categories: success, error, info, warning."""
    if len(message) > MAX_FLASH_CHARS:
        message = message[: MAX_FLASH_CHARS - 1].rstrip() + "…"
    flashes = [*request.session.get(FLASH_KEY, []), [category, message]][-MAX_FLASHES:]
    while len(flashes) > 1 and len(json.dumps(flashes)) > MAX_FLASH_BYTES:
        flashes.pop(0)
    request.session[FLASH_KEY] = flashes


def pop_flashes(request: Request) -> list[tuple[str, str]]:
    raw = request.session.pop(FLASH_KEY, None) or []
    return [(str(c), str(m)) for c, m in raw if m]


def remember_company(request: Request, company_id: int) -> None:
    if request.session.get(COMPANY_KEY) != company_id:
        request.session[COMPANY_KEY] = company_id


def safe_next(target: object, default: str = "/") -> str:
    """Only allow redirects to a local path (no scheme, no //host, no header injection)."""
    if not isinstance(target, str) or not target.startswith("/") or target.startswith("//"):
        return default
    if any(ch in target for ch in ("\\", "\r", "\n", "\t")):
        return default
    return target
