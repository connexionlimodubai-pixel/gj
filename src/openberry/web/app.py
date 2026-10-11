"""FastAPI application: dashboard pages, JSON API, static files, scheduler and the HTTP MCP endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import secrets
import sqlite3
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
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import __version__, db, repo
from ..config import Settings, get_settings, set_settings
from . import api, auth, data, keys, leads, mail, pages, scans
from .ratelimit import Limits
from .ui import STATIC_DIR, render

log = logging.getLogger(__name__)

SESSION_COOKIE = "openberry_session"
SESSION_MAX_AGE = 14 * 24 * 3600
SCHEDULER_STOP_TIMEOUT = 5.0
EMAIL_STOP_TIMEOUT = 30.0
LifespanHook = Callable[[], AbstractAsyncContextManager[Any]]

ERROR_TITLES = {
    400: "That didn't work", 401: "Please log in", 403: "Not allowed", 404: "Page not found",
    405: "Not allowed", 413: "That upload is too large", 422: "Check your input", 429: "Slow down",
    500: "Something went wrong",
}


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith(("/api", "/mcp", "/static", "/healthz"))


def _error_page(request: Request, status_code: int, message: str, title: str = "", public: bool = False,
                data_link: bool = False) -> Response:
    """The error page. `public` leaves out the menu, which reads the database: for errors about the database, which
    also link to Your data (`data_link`)."""
    return render(request, "error.html", {
        "title": title or ERROR_TITLES.get(status_code, "Error"), "status_code": status_code, "message": message,
        "data_link": data_link,
    }, status_code=status_code, public=public)


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


def _database_problem(request: Request, exc: db.DatabaseProblem) -> Response:
    """A data file this version can't open (written by a newer version, or an upgrade that couldn't save a copy
    first): say so and what to do, on every page, instead of a bare error."""
    db.report_once(log, f"{request.method} {request.url.path}", exc)
    if not _wants_html(request):
        return JSONResponse({"detail": str(exc)}, status_code=503)
    return _safe_error_page(request, 503, str(exc), title="OpenBerry can't open your data", data_link=True)


GENERIC_ERROR = "Something unexpected went wrong. Try again. If it happens again, the details are in {log}."


def _safe_error_page(request: Request, status_code: int, message: str, title: str = "",
                     data_link: bool = False) -> Response:
    """The error page without the menu (it reads the database), or a plain page if even that can't be shown."""
    title = title or ERROR_TITLES.get(status_code, "Error")
    try:
        return _error_page(request, status_code, message, title=title, public=True, data_link=data_link)
    except Exception:
        log.exception("could not show the error page")
        return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)} · OpenBerry</title>"
                            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><p><a href='/'>Go to the "
                            "dashboard</a></p>", status_code=status_code)


DATABASE_TITLES = {db.DISK_FULL: "Your disk is full", db.DAMAGED: "OpenBerry can't read your data",
                   db.BUSY: "OpenBerry is busy"}


def _database_error(request: Request, exc: sqlite3.Error) -> Response:
    """A full disk, a damaged file or a busy database: what happened and what to do, in plain words. Any other
    SQLite error is a bug: the generic page."""
    message = db.problem_text(exc)
    if message is None:
        return _server_error(request, exc)
    db.report_once(log, f"{request.method} {request.url.path}", exc)
    status = 503 if message.startswith(db.BUSY) else 500
    if not _wants_html(request):
        return JSONResponse({"detail": message}, status_code=status)
    title = next(t for prefix, t in DATABASE_TITLES.items() if message.startswith(prefix))
    return _safe_error_page(request, status, message, title=title, data_link=status == 500)


def _with_security_headers(response: Response) -> Response:
    """ServerErrorMiddleware answers outside SecurityHeadersMiddleware: the headers are set here instead."""
    for key, value in auth.SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def _server_error(request: Request, exc: Exception) -> Response:
    """Any error nothing else handled: a plain page (JSON under /api) with no traceback, which goes to the log."""
    message = db.problem_text(exc) if isinstance(exc, (OSError, sqlite3.Error)) else None
    if message is None:  # uvicorn logs the traceback too: ServerErrorMiddleware raises it again after this answer
        log.error("%s %s failed: %s: %s", request.method, request.url.path, type(exc).__name__, exc)
        message = GENERIC_ERROR.format(log=db.log_location())
    if not _wants_html(request):
        return _with_security_headers(JSONResponse({"detail": message}, status_code=500))
    return _with_security_headers(_safe_error_page(request, 500, message, data_link=isinstance(exc, OSError)))


def _too_large_id(request: Request, exc: OverflowError) -> Response:
    """An id larger than SQLite's integers (/c/99999999999999999999999): no such page, not a crash."""
    if not _wants_html(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return _error_page(request, 404, "There's no page at this address.")


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
        email_task: asyncio.Task[None] | None = None
        app.state.email_wake = None
        if problem := await asyncio.to_thread(db.quick_check):  # once per process (the desktop app checked already)
            log.error("the database failed SQLite's quick check (%s). %s", problem, db.damaged_text())
        if settings.scheduler_enabled:
            from ..scheduler import email_loop, scheduler_loop

            scheduler_task = asyncio.create_task(scheduler_loop(stop), name="openberry-scheduler")
            # Email from the user's mailbox: only this process (the dashboard, the desktop app) sends, never
            # `openberry mcp`. The dashboard's Send now sets email_wake.
            app.state.email_wake = asyncio.Event()
            email_task = asyncio.create_task(email_loop(stop, app.state.email_wake), name="openberry-email")
        try:
            yield
        finally:
            stop.set()
            if scheduler_task is not None:
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(scheduler_task, SCHEDULER_STOP_TIMEOUT)
            if email_task is not None:  # an email being sent finishes first (its thread can't be stopped anyway)
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(email_task, EMAIL_STOP_TIMEOUT)
            app.state.email_wake = None
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
    app.state.email_wake = None  # set by _lifespan while the email loop runs (Send now wakes it)
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
    app.add_exception_handler(db.DatabaseProblem, _database_problem)
    app.add_exception_handler(sqlite3.Error, _database_error)
    app.add_exception_handler(OverflowError, _too_large_id)
    app.add_exception_handler(Exception, _server_error)  # ServerErrorMiddleware: outside the other middleware

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        """Up only if the database answers. "app" tells OpenBerry apart from another program's health check on the
        same port (desktop.py), so it is in the 503 answer too."""
        try:
            with db.connect() as conn:
                conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # reads the file, unlike SELECT 1
        except (sqlite3.Error, db.DatabaseProblem, OSError) as exc:
            db.report_once(log, "health check", exc)
            return JSONResponse({"ok": False, "app": "openberry", "db": str(exc)}, status_code=503)
        return JSONResponse({"ok": True, "app": "openberry"})

    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(pages.public_router)
    app.include_router(pages.router)
    app.include_router(keys.router)
    app.include_router(mail.router)
    app.include_router(data.router)
    app.include_router(leads.router)

    if settings.http_mcp_enabled:
        try:
            from ..mcp_server import mount_http
        except ImportError as exc:
            log.warning("HTTP MCP endpoint disabled: %s", exc)
        else:
            mount_http(app, settings)
    return app
