"""Login, CSRF protection, JSON API authentication and security headers.

- Dashboard pages need a logged-in session when OPENBERRY_PASSWORD is set.
- Every state-changing form carries the session's CSRF token (`checked_form`).
- The JSON API takes `Authorization: Bearer <OPENBERRY_API_TOKEN>`. The dashboard's own
  fetch() calls authenticate with the session plus an `X-CSRF-Token` header instead.
- Local mode (no password) has no login, so two browser attacks are blocked explicitly: other
  websites making the visitor's browser write through the open API (cross-site requests), and
  DNS rebinding (an attacker's domain re-pointed at 127.0.0.1 to read pages as same-origin).
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import FormData, Headers, MutableHeaders
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings, get_settings
from ..models import split_list
from .session import csrf_valid, is_logged_in, log_in, safe_next
from .ui import render

SITE_SUMMARY_PATH = "/api/site-summary"
FAILED_LOGIN_DELAY = 0.5
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# /healthz serves container health checks (any Host); /mcp applies the same Host policy itself.
HOST_CHECK_EXEMPT = ("/healthz", "/mcp")
MAX_BODY_BYTES = 8 * 1024 * 1024  # the 5 MB CSV import plus the form around it
BODY_LIMIT_EXEMPT = ("/mcp",)

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


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


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


def _from_browser(request: Request) -> bool:
    """Browsers send Origin with every POST and Sec-Fetch-Site with every request; scripts send neither."""
    return "origin" in request.headers or "sec-fetch-site" in request.headers


def require_api_auth(request: Request) -> None:
    """Bearer token for scripts; session + X-CSRF-Token header for the dashboard's own calls.

    Local mode (no password and no API token) leaves the API open to scripts, as the dashboard
    is. A browser may still only change data with the dashboard's CSRF header, so another
    website can't make the visitor's browser start scans or add data.
    """
    settings = get_settings()
    if _bearer_ok(request, settings):
        return
    browser = csrf_valid(request, request.headers.get("x-csrf-token"))
    if not settings.password and not settings.api_token:
        if browser or request.method in SAFE_METHODS or not _from_browser(request):
            return
        raise HTTPException(403, "Cross-site request refused: call the API from a script, or from the "
                                 "dashboard with its X-CSRF-Token header.")
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


# --------------------------------------------------------------------------------------
# Host check for local mode (DNS-rebinding defence)
# --------------------------------------------------------------------------------------

def _hostname(value: str) -> str:
    """'Example.com:8000' -> 'example.com', '[::1]:80' -> '::1', 'https://x.io/' -> 'x.io'; '' if unparsable."""
    value = value.strip()
    try:
        return (urlparse(value if "://" in value else f"//{value}").hostname or "").lower()
    except ValueError:
        return ""


def host_allowed(host_header: str, settings: Settings) -> bool:
    """Whether a Host header is one a DNS-rebinding page cannot produce.

    Accepted: IP addresses (a rebinding attack needs a domain name), localhost, the host of
    OPENBERRY_BASE_URL and OPENBERRY_ALLOWED_HOSTS ('*' accepts everything), the same list
    the HTTP MCP endpoint uses.
    """
    extra = split_list(settings.allowed_hosts)
    if "*" in extra:
        return True
    name = _hostname(host_header)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in {*LOCAL_HOSTS, _hostname(settings.base_url), *(_hostname(e) for e in extra)}


class LocalHostGuardMiddleware:
    """Without a password, answer only to expected Host names (see `host_allowed`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not get_settings().password and not _under(scope["path"], HOST_CHECK_EXEMPT):
            host = Headers(scope=scope).get("host")
            if host is not None and not host_allowed(host, get_settings()):
                response = PlainTextResponse(
                    f"OpenBerry has no password, so it only answers on localhost, IP addresses, the host of "
                    f"OPENBERRY_BASE_URL and OPENBERRY_ALLOWED_HOSTS, not on '{host}'. Add this host to "
                    "OPENBERRY_ALLOWED_HOSTS, or set OPENBERRY_PASSWORD.", status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Refuse request bodies over MAX_BODY_BYTES with 413 before they are parsed.

    /login and public /register take anonymous POSTs, and Starlette spools uploaded files to
    disk with no size limit. Chunked bodies without Content-Length are counted as they arrive.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _under(scope["path"], BODY_LIMIT_EXEMPT):
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            await PlainTextResponse("Request body too large.", status_code=413)(scope, receive, send)
            return
        received = 0

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise HTTPException(413, "Request body too large.")
            return message

        await self.app(scope, counted_receive, send)
