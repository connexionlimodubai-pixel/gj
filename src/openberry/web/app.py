"""FastAPI application: dashboard pages, JSON API, static files, scheduler and the HTTP MCP endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from .. import __version__, repo
from ..config import Settings, get_settings, set_settings
from . import api, auth, keys, leads, pages, scans
from .ratelimit import Limits
from .ui import STATIC_DIR, render

log = logging.getLogger(__name__)

SESSION_COOKIE = "openberry_session"
SESSION_MAX_AGE = 14 * 24 * 3600
SCHEDULER_STOP_TIMEOUT = 5.0
LifespanHook = Callable[[], AbstractAsyncContextManager[Any]]

ERROR_TITLES = {
    400: "That didn't work", 401: "Please log in", 403: "Not allowed", 404: "Page not found",
    405: "Not allowed", 413: "That upload is too large", 422: "Check your input", 429: "Slow down",
    500: "Something went wrong",
}


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith(("/api", "/mcp", "/static", "/healthz"))


def _error_page(request: Request, status_code: int, message: str) -> Response:
    return render(request, "error.html", {
        "title": ERROR_TITLES.get(status_code, "Error"), "status_code": status_code, "message": message,
    }, status_code=status_code)


def _login_required(request: Request, exc: auth.LoginRequired) -> Response:
    return RedirectResponse(f"/login?next={quote(exc.next_url, safe='/')}", status_code=303)


def _not_found(request: Request, exc: repo.NotFound) -> Response:
    message = str(exc) or "Not found"
    if not _wants_html(request):
        return JSONResponse({"detail": message}, status_code=404)
    return _error_page(request, 404, "We couldn't find that. It may have been deleted. " + message.capitalize() + ".")


def _value_error(request: Request, exc: ValueError) -> Response:
    if isinstance(exc, ValidationError):
        detail: Any = exc.errors(include_url=False, include_context=False, include_input=False)
    else:
        detail = str(exc)
    if not _wants_html(request):
        return JSONResponse({"detail": detail}, status_code=422)
    return _error_page(request, 400, str(exc) if isinstance(detail, str) else "Some of the input was not valid.")


def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if not _wants_html(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    detail = exc.detail if isinstance(exc.detail, str) else ""
    if exc.status_code == 404:
        detail = "There's no page at this address."
    return _error_page(request, exc.status_code, detail)


def _json_errors(errors: Any) -> list[dict[str, Any]]:
    """type/loc/msg of each validation error; the raw input (bytes) and context (exceptions) may not be JSON."""
    return [{key: err[key] for key in ("type", "loc", "msg") if key in err} for err in errors]


def _request_validation_error(request: Request, exc: RequestValidationError) -> Response:
    if not _wants_html(request):
        return JSONResponse({"detail": _json_errors(exc.errors())}, status_code=422)
    return _error_page(request, 404, "There's no page at this address.")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    async with AsyncExitStack() as stack:
        for hook in list(app.state.lifespan_hooks):
            await stack.enter_async_context(hook())
        stop = asyncio.Event()
        scheduler_task: asyncio.Task[None] | None = None
        if settings.scheduler_enabled:
            from ..scheduler import scheduler_loop

            scheduler_task = asyncio.create_task(scheduler_loop(stop), name="openberry-scheduler")
        try:
            yield
        finally:
            stop.set()
            if scheduler_task is not None:
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(scheduler_task, SCHEDULER_STOP_TIMEOUT)
            await scans.cancel_all()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the dashboard app. Passing `settings` also makes them the process-wide settings."""
    if settings is None:
        settings = get_settings()
    else:
        set_settings(settings)
    if not settings.secret_key:
        settings.secret_key = secrets.token_hex(32)

    app = FastAPI(
        title="OpenBerry", version=__version__, lifespan=_lifespan,
        description="Intent-signal lead generation. JSON API: send `Authorization: Bearer $OPENBERRY_API_TOKEN`.",
        docs_url=None, redoc_url=None, openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.limits = Limits()  # login and anonymous-form rate limits (see ratelimit.py)
    hooks: list[LifespanHook] = []  # entered in _lifespan; mcp_server.mount_http appends to it
    app.state.lifespan_hooks = hooks

    # Innermost first: refusals from the host guard and the body limit still get the security headers.
    app.add_middleware(auth.BodySizeLimitMiddleware)
    app.add_middleware(auth.LocalHostGuardMiddleware)
    app.add_middleware(auth.SecurityHeadersMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie=SESSION_COOKIE,
                       max_age=SESSION_MAX_AGE, same_site="lax",
                       https_only=settings.base_url.lower().startswith("https://"))

    app.add_exception_handler(auth.LoginRequired, _login_required)
    app.add_exception_handler(repo.NotFound, _not_found)
    app.add_exception_handler(ValueError, _value_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, bool | str]:
        # "app" tells OpenBerry apart from another program's health check on the same port (desktop.py).
        return {"ok": True, "app": "openberry"}

    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(pages.public_router)
    app.include_router(pages.router)
    app.include_router(keys.router)
    app.include_router(leads.router)

    if settings.http_mcp_enabled:
        try:
            from ..mcp_server import mount_http
        except ImportError as exc:
            log.warning("HTTP MCP endpoint disabled: %s", exc)
        else:
            mount_http(app, settings)
    return app
