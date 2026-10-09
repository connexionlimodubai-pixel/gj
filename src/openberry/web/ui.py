"""Template environment, Jinja filters and the page-rendering helper."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from .. import __version__, repo
from ..config import get_settings
from ..models import (
    COMPANY_SIZES,
    COMPANY_TYPES,
    LEAD_STATUSES,
    MESSAGE_CHANNELS,
    SENIORITIES,
    SIGNAL_SOURCES,
    SIGNAL_TYPES,
    TIERS,
    TONES,
    Company,
    Lead,
)
from ..outreach import (
    LINKEDIN_CONNECT_LIMIT,
    LINKEDIN_CONNECT_LIMIT_FREE,
    LINKEDIN_FREE_NOTES_PER_MONTH,
    account_label,
    connect_note_limit,
)
from .forms import AGENT_LIMIT_RANGE
from .icons import ICONS, LOGO
from .session import csrf_token, is_logged_in, pop_flashes

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
REPO_URL = "https://github.com/connexionlimodubai-pixel/gj"
DOCS_URL = REPO_URL + "#readme"
AGENT_DOCS_URL = REPO_URL + "/blob/main/docs/AI_AGENT_SENDING.md"

CHANNEL_LABELS = {
    "linkedin_connect": "LinkedIn connection note",
    "linkedin_dm": "LinkedIn message",
    "email": "Email",
    "other": "Other",
}
SOURCE_LABELS = {
    "hackernews": "Hacker News", "reddit": "Reddit", "github": "GitHub", "greenhouse": "Greenhouse",
    "lever": "Lever", "ashby": "Ashby", "google_news": "Google News", "rss": "RSS", "linkedin": "LinkedIn",
    "web": "Web", "manual": "Manual", "claude": "Claude", "csv": "CSV import", "demo": "Demo",
    "sec_edgar": "SEC EDGAR",
}
LEAD_SOURCES = SIGNAL_SOURCES
# ScanRun.status -> label; other statuses are shown humanized with a neutral badge.
SCAN_STATUS_LABELS = {"running": "Running", "ok": "Ok", "failed": "Failed",
                      "nothing_configured": "No sources configured", "no_data": "No data"}
STATUS_LABELS = {s: s.replace("_", " ").capitalize() for s in LEAD_STATUSES}
LINKEDIN_ACCOUNT_OPTIONS = [("free", "Free (Basic)"), ("premium", "Premium")]


def _asset_version() -> str:
    digest = hashlib.sha1(__version__.encode())
    for path in sorted(STATIC_DIR.glob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------

def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    """'just now', '5m ago', '3h ago', '3d ago', '2mo ago', 'in 2d'."""
    dt = as_utc(dt)
    if dt is None:
        return "never"
    now = now or datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    future = secs < -60
    secs = abs(secs)
    if secs < 60:
        return "just now"
    if secs < 3600:
        text = f"{int(secs // 60)}m"
    elif secs < 86400:
        text = f"{int(secs // 3600)}h"
    elif secs < 86400 * 60:
        text = f"{int(secs // 86400)}d"
    elif secs < 86400 * 365:
        text = f"{int(secs // (86400 * 30))}mo"
    else:
        text = f"{int(secs // (86400 * 365))}y"
    return f"in {text}" if future else f"{text} ago"


def abs_dt(dt: datetime | None) -> str:
    dt = as_utc(dt)
    return f"{dt.day} {dt:%b %Y, %H:%M} UTC" if dt else ""


def iso_dt(dt: datetime | None) -> str:
    dt = as_utc(dt)
    return dt.isoformat() if dt else ""


def compact(value: Any) -> str:
    """1,284 / 12.9K / 4.2M; None -> an em dash."""
    if value is None:
        return "—"
    n = float(value)
    if abs(n) < 10_000:
        return f"{int(n):,}" if n == int(n) else f"{n:,.1f}"
    if abs(n) < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


_HOSTLIKE = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?$", re.I)


def safe_url(url: Any) -> str:
    """Return an http(s) URL safe for an href, or '' (blocks javascript:, data: and friends)."""
    url = str(url or "").strip()
    if not url or any(ch in url for ch in "\r\n\t "):
        return ""
    if re.match(r"^https?://", url, re.I):
        return url
    if _HOSTLIKE.match(url):
        return "https://" + url
    return ""


def mailto(email: Any) -> str:
    email = str(email or "").strip()
    if re.fullmatch(r"[^@\s<>\"'()]+@[^@\s<>\"'()]+\.[^@\s<>\"'()]+", email):
        return "mailto:" + email
    return ""


def host(url: Any) -> str:
    parsed = urlparse(safe_url(url))
    return (parsed.netloc or "").removeprefix("www.")


def reason_parts(reason: str) -> tuple[str, str]:
    """Split a scoring reason into (kind, text); kinds map to icons in the templates."""
    reason = (reason or "").strip()
    if reason.startswith("AI"):
        return "ai", "AI score " + reason[2:].strip()
    kind = {"+": "match", "-": "miss", "?": "unknown", "!": "block", "*": "signal"}.get(reason[:1])
    if kind:
        return kind, reason[1:].strip()
    return "info", reason


def top_reason(lead: Lead) -> tuple[str, str] | None:
    """The single most telling reason for a table row: newest strong signal, else best ICP match."""
    reasons = [reason_parts(r) for r in lead.score_reasons]
    concrete = [(k, t) for k, t in reasons if not t.startswith(("+", "Signal stacking"))]
    for wanted in ("block", "signal", "match"):
        for kind, text in concrete:
            if kind == wanted:
                return kind, text
    return reasons[0] if reasons else None


def pending_review(company: Company) -> bool:
    """Paused since it was registered and never edited or scanned: how public registrations arrive.

    (A company created paused through the API or Claude shows the same way until it is edited.)
    """
    return (company.status == "paused" and company.last_scan_at is None
            and as_utc(company.created_at) == as_utc(company.updated_at))


def company_status_label(company: Company) -> str:
    if pending_review(company):
        return "Pending review"
    return "Active" if company.status == "active" else "Paused"


def scan_status_label(status: str) -> str:
    return SCAN_STATUS_LABELS.get(status) or (status or "unknown").replace("_", " ").capitalize()


# Message.sent_via -> who recorded the send, shown next to sent messages ("" = the user, shown as nothing).
SENT_VIA_LABELS = {"agent": "Sent by AI agent", "claude": "Marked sent by Claude"}


def sent_via_label(message: Any) -> str:
    """'Sent by AI agent' for a message the user's own AI agent confirmed as sent (repo.send_queue), etc.

    Messages sent by hand, or stored before agent sending existed, have no marker and get ''.
    """
    return SENT_VIA_LABELS.get(str(getattr(message, "sent_via", "") or "").strip().lower(), "")


def initials(name: str) -> str:
    words = [w for w in re.split(r"[\s\-_.]+", name or "") if w and w[0].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def signal_label(signal_type: str) -> str:
    return SIGNAL_TYPES.get(signal_type, SIGNAL_TYPES["custom"])[0]


def select_options(options: Any, current: Any) -> list[tuple[str, str, bool]]:
    """(value, label, selected) for a <select>.

    A stored value that is not one of the options (set through the API or by Claude) is kept as
    an extra selected option; otherwise the browser would submit the first option and saving the
    form would silently replace it. Case differences select the matching option.
    """
    opts = [(str(v), str(t)) for v, t in options]
    current = str(current or "")
    match = next((v for v, _ in opts if v == current), None)
    if match is None:
        match = next((v for v, _ in opts if v.lower() == current.lower()), None) if current else None
    out = [(v, t, v == match) for v, t in opts]
    if current and match is None:
        out.append((current, current, True))
    return out


def check_options(options: Any, chosen: Any) -> list[tuple[str, str, bool]]:
    """(value, label, checked) for a checkbox group; chosen values that aren't options stay as extra boxes."""
    chosen = [str(c) for c in chosen or []]
    picked = {c.lower() for c in chosen}
    opts = [(str(v), str(t)) for v, t in options]
    known = {v.lower() for v, _ in opts}
    return [(v, t, v.lower() in picked) for v, t in opts] + [(c, c, True) for c in chosen if c.lower() not in known]


def query_with(request: Request, **overrides: Any) -> str:
    """Current query string with some parameters replaced ('' or None removes them)."""
    params = {k: v for k, v in request.query_params.items()}
    for key, value in overrides.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    if params.get("page") == "1":
        params.pop("page")
    return "?" + urlencode(params) if params else "?"


def _build_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        ago=ago, abs_dt=abs_dt, iso_dt=iso_dt, compact=compact, safe_url=safe_url, mailto=mailto, host=host,
        reason_parts=reason_parts, top_reason=top_reason, initials=initials, plural=plural,
        signal_label=signal_label,
        channel_label=lambda c: CHANNEL_LABELS.get(c, c),
        source_label=lambda s: SOURCE_LABELS.get(s, (s or "").replace("_", " ").title()),
        status_label=lambda s: STATUS_LABELS.get(s, (s or "").capitalize()),
        company_status=company_status_label, scan_status=scan_status_label, sent_via=sent_via_label,
    )
    env.globals.update(
        SIGNAL_TYPES=SIGNAL_TYPES, SENIORITIES=SENIORITIES, COMPANY_SIZES=COMPANY_SIZES, COMPANY_TYPES=COMPANY_TYPES,
        TONES=TONES, LEAD_STATUSES=LEAD_STATUSES, MESSAGE_CHANNELS=MESSAGE_CHANNELS, TIERS=TIERS,
        LEAD_SOURCES=LEAD_SOURCES, SIGNAL_SOURCES=SIGNAL_SOURCES, CHANNEL_LABELS=CHANNEL_LABELS,
        LINKEDIN_CONNECT_LIMIT=LINKEDIN_CONNECT_LIMIT, LINKEDIN_CONNECT_LIMIT_FREE=LINKEDIN_CONNECT_LIMIT_FREE,
        LINKEDIN_FREE_NOTES_PER_MONTH=LINKEDIN_FREE_NOTES_PER_MONTH,
        # The longest connection note the company's LinkedIn account takes (200 free, 300 Premium), and its name.
        connect_note_limit=connect_note_limit, linkedin_account_label=account_label,
        LINKEDIN_ACCOUNT_OPTIONS=LINKEDIN_ACCOUNT_OPTIONS,
        query_with=query_with, version=__version__,
        select_options=select_options, check_options=check_options, pending_review=pending_review,
        SCAN_STATUS_LABELS=SCAN_STATUS_LABELS,
        DOCS_URL=DOCS_URL, AGENT_DOCS_URL=AGENT_DOCS_URL, ICONS=ICONS, LOGO=LOGO,
        AGENT_LIMIT_RANGE=AGENT_LIMIT_RANGE,
        message_version=repo.message_version, BULK_MESSAGES_MAX=repo.BULK_MESSAGES_MAX,
        SIZE_OPTIONS=[(s, f"{s} employees") for s in COMPANY_SIZES],
        SIZE_CHIPS=[(s, s) for s in COMPANY_SIZES],
        COMPANY_TYPE_OPTIONS=[(t, "SMB" if t == "smb" else t.capitalize()) for t in COMPANY_TYPES],
        TONE_OPTIONS=[(t, t.capitalize()) for t in TONES],
        SIGNAL_OPTIONS=[(k, label) for k, (label, _) in SIGNAL_TYPES.items()],
        CHANNEL_OPTIONS=list(CHANNEL_LABELS.items()),
        STATUS_OPTIONS=[(s, STATUS_LABELS[s]) for s in LEAD_STATUSES],
    )
    return env


templates = Jinja2Templates(env=_build_env())
ASSET_VERSION = _asset_version()


def render(request: Request, name: str, context: dict[str, Any] | None = None, *, status_code: int = 200,
           public: bool = False, headers: dict[str, str] | None = None) -> Response:
    """Render a page with the shared shell context (nav, flashes, CSRF token, mode flags).

    `public=True` renders without the sidebar or any company data (login, public registration).
    Touches the database for the company switcher, so call it from sync handlers or a threadpool.
    """
    settings = get_settings()
    logged_in = is_logged_in(request)
    show_nav = logged_in and not public
    ctx: dict[str, Any] = {"company": None, "active": "", "title": "", "values": {}, "errors": {}}
    ctx.update(context or {})
    ctx.update(
        show_nav=show_nav,
        public=public,
        logged_in=logged_in,
        auth_enabled=bool(settings.password),
        nav_companies=repo.list_companies() if show_nav else [],
        flashes=pop_flashes(request),
        csrf_token=csrf_token(request),
        asset_version=ASSET_VERSION,
        ollama_enabled=bool(settings.ollama_url),
        base_url=settings.base_url,
        public_registration=settings.public_registration,
        scheduler_enabled=settings.scheduler_enabled,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)


# --------------------------------------------------------------------------------------
# Query-string helpers shared by the page routers
# --------------------------------------------------------------------------------------

PAGE_SIZE = 50


def int_param(value: Any, default: int = 1, low: int = 1, high: int = 10_000) -> int:
    try:
        return max(low, min(high, int(str(value))))
    except (TypeError, ValueError):
        return default


def choice(value: Any, allowed: Any) -> str | None:
    """`value` if it is one of `allowed`, else None (invalid filters are ignored, not errors)."""
    return value if isinstance(value, str) and value in allowed else None


def page_info(page: int, total: int, size: int = PAGE_SIZE) -> dict[str, int]:
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    return {
        "page": page, "pages": pages, "total": total, "size": size, "offset": (page - 1) * size,
        "start": (page - 1) * size + 1 if total else 0, "end": min(page * size, total),
    }
