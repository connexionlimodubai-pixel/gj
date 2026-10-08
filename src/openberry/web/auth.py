"""Login, CSRF protection, JSON API authentication and security headers.

- Dashboard pages need a logged-in session when OPENBERRY_PASSWORD is set.
- Every state-changing form carries the session's CSRF token (`checked_form`).
- The JSON API takes `Authorization: Bearer <OPENBERRY_API_TOKEN>`. The dashboard's own
  fetch() calls authenticate with the session plus an `X-CSRF-Token` header instead.
"""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import FormData, MutableHeaders
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings, get_settings
from .session import csrf_valid, is_logged_in, log_in, safe_next
from .ui import render

SITE_SUMMARY_PATH = "/api/site-summary"
FAILED_LOGIN_DELAY = 0.5

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "content-security-policy": CONTENT_SECURITY_POLICY,
}


class LoginRequired(Exception):
    """Raised by `require_login`; the app turns it into a redirect to /login."""

    def __init__(self, next_url: str = "/") -> None:
        super().__init__(next_url)
        self.next_url = next_url


def require_login(request: Request) -> None:
    if not is_logged_in(request):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(target if request.method == "GET" else "/")


def public_registration_open(request: Request) -> bool:
    """Anonymous visitors may use the registration form (agency intake mode)."""
    return get_settings().public_registration and not is_logged_in(request)


async def checked_form(request: Request) -> FormData:
    """Parse the submitted form and verify its CSRF token (403 when missing or wrong)."""
    form = await request.form()
    token = form.get("csrf_token") or request.headers.get("x-csrf-token")
    if not csrf_valid(request, token):
        raise HTTPException(403, "This form has expired. Reload the page and try again.")
    return form


def _bearer_ok(request: Request, settings: Settings) -> bool:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    return (scheme.lower() == "bearer" and bool(settings.api_token) and bool(token)
            and hmac.compare_digest(token.encode(), settings.api_token.encode()))


def require_api_auth(request: Request) -> None:
    """Bearer token for scripts; session + X-CSRF-Token header for the dashboard's own calls.

    Local mode (no password and no API token) leaves the API open, as the dashboard is.
    """
    settings = get_settings()
    if _bearer_ok(request, settings):
        return
    if not settings.password and not settings.api_token:
        return
    browser = csrf_valid(request, request.headers.get("x-csrf-token"))
    if browser and (is_logged_in(request) or (
            settings.public_registration and request.url.path == SITE_SUMMARY_PATH)):
        return
    detail = ("Missing or invalid bearer token." if settings.api_token
              else "The API is disabled: set OPENBERRY_API_TOKEN to use it in server mode.")
    raise HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


# --------------------------------------------------------------------------------------
# Login / logout
# --------------------------------------------------------------------------------------

router = APIRouter()


@router.get("/login", include_in_schema=False)
def login_page(request: Request, next: str = "/") -> Response:
    if not get_settings().password:
        return RedirectResponse("/", status_code=303)
    if is_logged_in(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next), "title": "Log in"}, public=True)


@router.post("/login", include_in_schema=False)
async def login(request: Request, form: FormData = Depends(checked_form)) -> Response:
    settings = get_settings()
    password = form.get("password")
    target = safe_next(form.get("next"))
    if not settings.password:
        return RedirectResponse("/", status_code=303)
    if isinstance(password, str) and hmac.compare_digest(password.encode(), settings.password.encode()):
        log_in(request)
        return RedirectResponse(target, status_code=303)
    await asyncio.sleep(FAILED_LOGIN_DELAY)  # slows down password guessing
    return render(request, "login.html", {"next": target, "error": "That password is not right.", "title": "Log in"},
                  status_code=401, public=True)


@router.post("/logout", include_in_schema=False)
def logout(request: Request, form: FormData = Depends(checked_form)) -> Response:
    request.session.clear()
    return RedirectResponse("/login" if get_settings().password else "/", status_code=303)


# --------------------------------------------------------------------------------------
# Security headers (pure ASGI so streaming responses such as /mcp are untouched)
# --------------------------------------------------------------------------------------

class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
