"""Job boards collector: public Greenhouse, Lever and Ashby postings -> account-level hiring signals.

Source (no key, no signup; one GET per configured board, and each board comes back whole)
    Greenhouse  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
                -> {"jobs": [...], "meta": {"total": n}}. content=true is requested because departments
                and offices only come with it; it also adds the description (HTML whose tags are
                entity-escaped, so it is unescaped twice) and makes large boards a few MB.
                Time: first_published; updated_at only as a fallback because it moves on every edit.
    Lever       GET https://api.lever.co/v0/postings/{site}?mode=json
                -> a bare JSON array. Time: createdAt (epoch milliseconds; observed in real payloads
                but not in Lever's README). EU-hosted sites live on api.eu.lever.co, so a 404 from the
                global host is retried once there.
    Ashby       GET https://api.ashbyhq.com/posting-api/job-board/{name}
                -> {"apiVersion": ..., "jobs": [...]}. Time: publishedAt. Postings with
                isListed == false are skipped.
    An unknown token gives a 404: the board is reported as not found and the scan goes on.

What it emits
    hiring  one account-level signal per open posting whose title matches signals.hiring_keywords
            (or icp.job_titles when no hiring keywords are set). account = the board's company name
            (else Greenhouse's company_name, else the token), lead = None: people later found at
            that company inherit the intent. account_domain stays empty: ATS boards rarely state
            the employer's own domain, and accounts are matched by name first.
    Titles are matched with scoring.phrase_in (every word of the keyword appears as a whole word,
    in any order) rather than base.find_terms (contiguous phrase): ATS titles often invert word
    order ("Manager, Travel", "Engineer, Data"), and it is the same rule ICP title scoring uses.

Strength (50 = typical)
    60 for a matching role, +10 for each additional matching open role at the same account in
    this scan, capped at 85: 1 role -> 60, 2 -> 70, 3 -> 80, 4+ -> 85. Every currently listed
    matching posting counts (not only the new ones): several open roles for one function means
    the account is building that team.

Time
    Only postings first published after ctx.since are returned. A posting without any usable
    timestamp is still returned (it is open now, which is the signal) with occurred_at = now and
    raw["undated"] = True. Its external_id is stable, so ingest stores it only once, the first
    time OpenBerry sees it, which makes "now" the best available estimate of when it appeared.

Limits and politeness
    None of the three APIs documents a GET rate limit. Boards are fetched one at a time with a short
    pause, at most MAX_BOARDS_PER_SCAN boards per scan (when more are configured the checked set moves
    on by MAX_BOARDS_PER_SCAN each day, so each board is still checked every few days, well inside
    the lookback window) and MAX_REQUESTS_PER_SCAN requests in total, Lever EU retries included.
    Each request has a wall-clock cap (request_timeout). A provider is
    skipped for the rest of the scan after HTTP 429, 401/403 (usually a firewall block), a zero
    rate-limit-remaining header or three failures in a row. Fetching also stops once ctx.max_items
    signals are found or the time budget (kept below the scan's per-collector timeout) runs out.
    There is no ETag cache (collectors keep no state between scans), so keep scan intervals in hours.
    Boards must be configured one by one (provider:token:Company): none of the APIs can search
    across companies.

Terms of use
    These are the public job-board APIs each ATS offers so that careers pages can embed their
    postings: Greenhouse ("Job Board data is publicly available"), Lever ("all job postings in the
    published state are publicly viewable") and Ashby's public posting API. Only GET requests are
    made and nothing is applied to. Link back to the posting and do not republish boards in bulk.
"""

from __future__ import annotations

import asyncio
import enum
import html
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..models import Company, JobBoard, SignalIn
from ..repo import company_key
from ..scoring import phrase_in
from .base import CollectContext, Collector, RawSignal, parse_time, strip_html, truncate

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{token}"
LEVER_EU_URL = "https://api.eu.lever.co/v0/postings/{token}"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"

MAX_BOARDS_PER_SCAN = 30
MAX_REQUESTS_PER_SCAN = 34        # one per board plus a few Lever EU retries
MAX_FAILURES_IN_A_ROW = 3         # per provider
BASE_STRENGTH = 60
EXTRA_ROLE_BONUS = 10
MAX_STRENGTH = 85
SUMMARY_LIMIT = 500
TITLE_LIMIT = 160
DESCRIPTION_LIMIT = 300
MAX_LOCATIONS = 3

_EMPLOYMENT = {"fulltime": "Full-time", "parttime": "Part-time", "intern": "Internship",
               "contract": "Contract", "temporary": "Temporary"}
_WORKPLACE = {"onsite": "On-site", "remote": "Remote", "hybrid": "Hybrid"}


class JobBoardsCollector(Collector):
    name = "jobs"
    label = "Job boards (Greenhouse / Lever / Ashby)"
    signal_types = ("hiring",)
    requires = "job_boards (plus hiring keywords or ICP job titles to match)"

    # Seconds between two requests, seconds after which no new request starts, and the wall-clock cap
    # on one request (body included: httpx timeouts are per read, so a slowly trickling multi-MB board
    # is not bounded by them). time_budget + request_timeout stays under
    # services.COLLECTOR_TIMEOUT_SECONDS, so a slow scan returns what it has instead of being cancelled.
    request_interval: float = 0.25
    time_budget: float = 90.0
    request_timeout: float = 25.0

    def is_configured(self, company: Company) -> bool:
        return bool(company.signals.job_boards)

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        if "hiring" not in company.signals.enabled_types or ctx.max_items <= 0:
            return []
        keywords, matched_from = hiring_terms(company)
        if not keywords:
            ctx.warn("Job boards: no hiring keywords or ICP job titles to match job titles against; "
                     "add some to get hiring signals")
            return []

        boards = select_boards(company.signals.job_boards, ctx)
        since = _utc(ctx.since)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        fetcher = _Fetcher(ctx, MAX_REQUESTS_PER_SCAN, self.request_interval, self.time_budget,
                           self.request_timeout)
        found: list[_Match] = []
        seen: set[str] = set()
        open_roles: Counter[str] = Counter()

        unchecked = 0
        for index, board in enumerate(boards):
            if len(found) >= ctx.max_items:
                unchecked = len(boards) - index
                break
            if (reason := fetcher.out_of_budget()) is not None:
                ctx.warn(f"Job boards: {reason}; {len(boards) - index} board(s) not checked this scan")
                break
            provider = PROVIDERS.get(board.provider)
            if provider is None:
                ctx.warn(f"Job boards: unknown provider '{board.provider}' for board '{board.token}'")
                continue
            if provider.name in fetcher.stopped:
                continue
            items = await fetcher.fetch_board(provider, board.token)
            if items is None:
                continue
            postings = matching_postings(provider, board.token, items, keywords, ctx)
            account = account_name(board, [p for p, _ in postings])
            open_roles[_account_key(account)] += len(postings)
            for posting, matched in postings:
                ext = external_id(provider.name, board.token, posting.job_id)
                if ext in seen:
                    continue
                seen.add(ext)
                if posting.published is not None and posting.published <= since:
                    continue
                found.append(_Match(provider.name, board.token, account, posting, matched))

        extra = len(found) - ctx.max_items
        if unchecked or extra > 0:
            dropped = [f"{extra} more matching role(s) dropped"] if extra > 0 else []
            dropped += [f"{unchecked} board(s) not checked"] if unchecked else []
            ctx.warn(f"Job boards: reached the limit of {ctx.max_items} signals; {' and '.join(dropped)} this scan")

        out: list[RawSignal] = []
        for m in found[: ctx.max_items]:
            try:
                out.append(build_signal(m, open_roles[_account_key(m.account)], matched_from, now))
            except Exception as exc:  # one odd posting must not sink the scan
                ctx.warn(f"{m.provider} board '{m.token}': skipped posting {m.posting.job_id} ({type(exc).__name__})")
        return out


# --------------------------------------------------------------------------------------
# Configuration helpers
# --------------------------------------------------------------------------------------


def hiring_terms(company: Company) -> tuple[list[str], str]:
    """Keywords to match job titles against, and which profile field they came from."""
    if company.signals.hiring_keywords:
        return list(company.signals.hiring_keywords), "hiring_keywords"
    if company.icp.job_titles:
        return list(company.icp.job_titles), "icp.job_titles"
    return [], ""


def select_boards(boards: list[JobBoard], ctx: CollectContext) -> list[JobBoard]:
    """Unique boards (provider + token, case-insensitive), at most MAX_BOARDS_PER_SCAN, rotated daily.

    The window moves by a whole MAX_BOARDS_PER_SCAN each day, so every board is checked at least once
    every ceil(n / MAX_BOARDS_PER_SCAN) days. Moving it by one board a day would leave boards
    unchecked for weeks, longer than the lookback window, and their new postings would never be seen.
    """
    unique: dict[tuple[str, str], JobBoard] = {}
    for board in boards:
        if not board.token.strip("."):
            # "." / ".." pass the model's token check, but httpx collapses them into another API path.
            ctx.warn(f"{board.provider} board '{board.token}': not a valid board token; skipped")
            continue
        unique.setdefault((board.provider, board.token.lower()), board)
    out = list(unique.values())
    if len(out) > MAX_BOARDS_PER_SCAN:
        start = (ctx.since.date().toordinal() * MAX_BOARDS_PER_SCAN) % len(out)
        out = out[start:] + out[:start]
        ctx.warn(f"Job boards: {len(out)} boards configured but only {MAX_BOARDS_PER_SCAN} are checked "
                 "per scan; the checked set rotates daily")
        out = out[:MAX_BOARDS_PER_SCAN]
    return out


def external_id(provider: str, token: str, job_id: str) -> str:
    return f"{provider}:{token.lower()}:{job_id}"


def role_strength(open_matching_roles: int) -> int:
    return min(MAX_STRENGTH, BASE_STRENGTH + EXTRA_ROLE_BONUS * max(0, open_matching_roles - 1))


def account_name(board: JobBoard, postings: list[Posting]) -> str:
    """The board's company name, else the name Greenhouse reports, else the token.

    models.parse_job_boards fills a missing company with the token ("greenhouse:discord" ->
    company "discord"), so a company equal to the token counts as unset and the reported name wins.
    """
    named = next((p.company_name for p in postings if p.company_name), "")
    company = board.company.strip()
    if company and company.lower() != board.token.lower():
        return company
    return named or company or board.token


def _account_key(name: str) -> str:
    """Same identity as an account lead (repo.company_key: case, punctuation and 'Ltd'/'Inc' ignored)."""
    return company_key(name) or name.strip().lower()


# --------------------------------------------------------------------------------------
# Postings: one normalised shape for the three providers
# --------------------------------------------------------------------------------------


@dataclass
class Posting:
    job_id: str
    title: str
    url: str
    published: datetime | None
    time_field: str = ""
    location: str = ""
    department: str = ""
    team: str = ""
    employment: str = ""
    workplace: str = ""
    description: str = ""
    company_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Match:
    provider: str
    token: str
    account: str
    posting: Posting
    matched: list[str]


def parse_greenhouse(job: dict[str, Any], token: str) -> Posting | None:
    job_id = _job_id(job.get("id"))
    published, time_field = _first_time(job, ("first_published", "updated_at"))
    location = job.get("location")
    location_name = _s(location.get("name")) if isinstance(location, dict) else ""
    offices = [_s(o.get("name")) for o in _dicts(job.get("offices"))]
    content = job.get("content")
    return Posting(
        job_id=job_id,
        title=_title(job.get("title")),
        url=_http_url(job.get("absolute_url")) or f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}",
        published=published,
        time_field=time_field,
        location=location_name or _join(offices),
        department=_join([_s(d.get("name")) for d in _dicts(job.get("departments"))]),
        description=strip_html(html.unescape(content)) if isinstance(content, str) else "",
        company_name=_s(job.get("company_name")),
        extra={"requisition_id": _s(job.get("requisition_id")), "updated_at": _s(job.get("updated_at"))},
    )


def parse_lever(item: dict[str, Any], token: str) -> Posting | None:
    job_id = _job_id(item.get("id"))
    published, time_field = _first_time(item, ("createdAt", "updatedAt"))
    cats = item.get("categories") if isinstance(item.get("categories"), dict) else {}
    description = (_s(item.get("descriptionPlain")) or _s(item.get("openingPlain"))
                   or strip_html(_text(item.get("description"))))
    return Posting(
        job_id=job_id,
        title=_title(item.get("text")),
        url=_http_url(item.get("hostedUrl")) or f"https://jobs.lever.co/{token}/{job_id}",
        published=published,
        time_field=time_field,
        location=_locations([cats.get("location"), *_list(cats.get("allLocations"))]),
        department=_s(cats.get("department")),
        team=_s(cats.get("team")),
        employment=_s(cats.get("commitment")),
        workplace=_workplace(item.get("workplaceType")),
        description=description,
        extra={"country": _s(item.get("country")), "salary": _lever_salary(item.get("salaryRange")),
               "apply_url": _http_url(item.get("applyUrl"))},
    )


def parse_ashby(item: dict[str, Any], token: str) -> Posting | None:
    if item.get("isListed") is False:
        return None
    job_url = _http_url(item.get("jobUrl"))
    # `id` is observed in every payload but missing from Ashby's field table: fall back to the URL slug.
    raw_id = item.get("id") or (urlsplit(job_url).path.rstrip("/").rsplit("/", 1)[-1] if job_url else None)
    job_id = _job_id(raw_id)
    published, time_field = _first_time(item, ("publishedAt", "updatedAt"))
    secondary = [s.get("location") for s in _dicts(item.get("secondaryLocations"))]
    employment = _s(item.get("employmentType"))
    return Posting(
        job_id=job_id,
        title=_title(item.get("title")),
        url=job_url or f"https://jobs.ashbyhq.com/{token}/{job_id}",
        published=published,
        time_field=time_field,
        location=_locations([item.get("location"), *secondary]),
        department=_s(item.get("department")),
        team=_s(item.get("team")),
        employment=_EMPLOYMENT.get(employment.lower(), employment),
        workplace=_workplace(item.get("workplaceType")) or ("Remote" if item.get("isRemote") is True else ""),
        description=_s(item.get("descriptionPlain")) or strip_html(_text(item.get("descriptionHtml"))),
        extra={"apply_url": _http_url(item.get("applyUrl"))},
    )


def matching_postings(provider: _Provider, token: str, items: list[Any], keywords: list[str],
                      ctx: CollectContext) -> list[tuple[Posting, list[str]]]:
    """Parse a board's items and keep the listed postings whose title matches, newest first."""
    out: dict[str, tuple[Posting, list[str]]] = {}
    malformed = 0
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError("posting is not an object")
            posting = provider.parse(item, token)
        except Exception:  # missing id/title, wrong types...: skip the posting, keep the board
            malformed += 1
            continue
        if posting is None or posting.job_id in out:
            continue
        matched = [k for k in keywords if phrase_in(k, posting.title)]
        if matched:
            out[posting.job_id] = (posting, matched)
    if malformed:
        ctx.warn(f"{provider.name} board '{token}': skipped {malformed} malformed posting(s)")
    return sorted(out.values(), key=lambda pm: _sort_key(pm[0]))


def _sort_key(posting: Posting) -> tuple[int, float]:
    if posting.published is None:
        return (1, 0.0)
    return (0, -posting.published.timestamp())


# --------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------


def build_signal(m: _Match, open_matching_roles: int, matched_from: str, now: datetime) -> RawSignal:
    p = m.posting
    raw: dict[str, Any] = {
        "provider": m.provider,
        "board": m.token,
        "job_id": p.job_id,
        "job_title": p.title,
        "matched": m.matched,
        "matched_from": matched_from,
        "location": p.location,
        "department": p.department,
        "team": p.team,
        "employment": p.employment,
        "workplace": p.workplace,
        "time_field": p.time_field,
        "open_matching_roles": open_matching_roles,
        **p.extra,
    }
    if p.published is None:
        raw["undated"] = True
    signal = SignalIn(
        type="hiring",
        title=truncate(f"Hiring: {p.title}", TITLE_LIMIT),
        summary=build_summary(m.account, p),
        url=p.url,
        source=m.provider,
        external_id=external_id(m.provider, m.token, p.job_id),
        strength=role_strength(open_matching_roles),
        occurred_at=p.published or now,
        raw={k: v for k, v in raw.items() if v not in (None, "", [], {})},
    )
    return RawSignal(signal=signal, account=m.account)


def build_summary(account: str, p: Posting) -> str:
    facts = [f"{label}: {value}" for label, value in
             (("Location", p.location), ("Department", p.department), ("Team", p.team)) if value]
    facts += [v for v in (p.employment, p.workplace) if v]
    text = f"{account} is hiring: {p.title}."
    if facts:
        text += " " + " · ".join(facts) + "."
    if p.description:
        text += " " + truncate(p.description, DESCRIPTION_LIMIT)
    return truncate(text, SUMMARY_LIMIT)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class _Outcome(enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass
class _Provider:
    name: str
    urls: tuple[str, ...]                   # tried in order while the previous one answers 404
    params: dict[str, str] | None
    extract: Callable[[Any], list[Any] | None]
    parse: Callable[[dict[str, Any], str], Posting | None]


def _jobs_key(data: Any) -> list[Any] | None:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return jobs if isinstance(jobs, list) else None


def _bare_list(data: Any) -> list[Any] | None:
    return data if isinstance(data, list) else None


PROVIDERS: dict[str, _Provider] = {
    "greenhouse": _Provider("greenhouse", (GREENHOUSE_URL,), {"content": "true"}, _jobs_key, parse_greenhouse),
    "lever": _Provider("lever", (LEVER_URL, LEVER_EU_URL), {"mode": "json"}, _bare_list, parse_lever),
    "ashby": _Provider("ashby", (ASHBY_URL,), None, _jobs_key, parse_ashby),
}


class _Fetcher:
    """Sequential, capped GETs with per-provider back-off. Never raises; warns instead."""

    def __init__(self, ctx: CollectContext, max_requests: int, interval: float, time_budget: float,
                 request_timeout: float) -> None:
        self.ctx = ctx
        self.max_requests = max_requests
        self.interval = interval
        self.request_timeout = request_timeout
        self.deadline = time.monotonic() + time_budget
        self.used = 0
        self.stopped: set[str] = set()
        self.failures: Counter[str] = Counter()

    def out_of_budget(self) -> str | None:
        if self.used >= self.max_requests:
            return f"request cap reached ({self.max_requests} per scan)"
        if time.monotonic() >= self.deadline:
            return "time budget for this scan used up"
        return None

    async def fetch_board(self, provider: _Provider, token: str) -> list[Any] | None:
        """The board's postings, or None (not found, failed, or out of budget; already warned)."""
        for template in provider.urls:
            if provider.name in self.stopped or self.out_of_budget() is not None:
                return None
            outcome, data = await self._get(provider, template.format(token=quote(token, safe="")), token)
            if outcome is _Outcome.NOT_FOUND:
                continue
            if outcome is _Outcome.FAILED:
                return None
            items = provider.extract(data)
            if items is None:
                self._fail(provider.name, f"{provider.name} board '{token}': unexpected response format")
            return items
        self.ctx.warn(f"{provider.name} board '{token}' not found")
        return None

    async def _get(self, provider: _Provider, url: str, token: str) -> tuple[_Outcome, Any]:
        name = provider.name
        if self.used and self.interval > 0:
            await asyncio.sleep(self.interval)
        self.used += 1
        try:
            resp = await asyncio.wait_for(
                self.ctx.client.get(url, params=provider.params, headers={"Accept": "application/json"}),
                self.request_timeout)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            self._fail(name, f"{name} board '{token}': request timed out")
            return _Outcome.FAILED, None
        except Exception as exc:  # transport errors, invalid URLs...: one board must not sink the scan
            self._fail(name, f"{name} board '{token}': request failed ({type(exc).__name__})")
            return _Outcome.FAILED, None

        status = resp.status_code
        if status == 404:
            self.failures[name] = 0
            return _Outcome.NOT_FOUND, None
        if status == 429:
            retry = resp.headers.get("retry-after", "").strip()
            hint = f", retry after {truncate(retry, 40)}" if retry else ""
            self._stop(name, f"{name}: rate limited (HTTP 429{hint}); skipped its remaining boards this scan")
            return _Outcome.FAILED, None
        if status in (401, 403):
            self._stop(name, f"{name} refused access (HTTP {status}) to board '{token}'; "
                             "skipped its remaining boards this scan")
            return _Outcome.FAILED, None
        if not resp.is_success:
            self._fail(name, f"{name} board '{token}': HTTP {status}")
            return _Outcome.FAILED, None
        try:
            data = resp.json()
        except ValueError:
            self._fail(name, f"{name} board '{token}': response was not valid JSON")
            return _Outcome.FAILED, None

        self.failures[name] = 0
        if _rate_limit_exhausted(resp.headers):
            self._stop(name, f"{name}: rate-limit budget exhausted; skipped its remaining boards this scan")
        if isinstance(data, dict) and data.get("ok") is False:
            return _Outcome.NOT_FOUND, None   # Lever's {"ok": false, "error": "Document not found"}
        return _Outcome.OK, data

    def _fail(self, provider: str, message: str) -> None:
        self.ctx.warn(message)
        self.failures[provider] += 1
        if self.failures[provider] >= MAX_FAILURES_IN_A_ROW and provider not in self.stopped:
            self._stop(provider, f"{provider}: {self.failures[provider]} failed requests in a row; "
                                 "skipped its remaining boards this scan")

    def _stop(self, provider: str, message: str) -> None:
        self.stopped.add(provider)
        self.ctx.warn(message)


def _rate_limit_exhausted(headers: httpx.Headers) -> bool:
    return any(headers.get(h, "").strip() == "0" for h in ("x-ratelimit-remaining", "ratelimit-remaining"))


# --------------------------------------------------------------------------------------
# Payload helpers (fields can be missing, null or of an unexpected type)
# --------------------------------------------------------------------------------------


def _s(value: Any) -> str:
    """A whitespace-collapsed string, or "" for missing, null and non-scalar values."""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _job_id(value: Any) -> str:
    job_id = _s(value)
    if not job_id:
        raise ValueError("posting without id")
    return job_id


def _title(value: Any) -> str:
    title = strip_html(_s(value))
    if not title:
        raise ValueError("posting without title")
    return title


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _join(values: list[str]) -> str:
    return ", ".join(dict.fromkeys(v for v in values if v))


def _locations(values: list[Any]) -> str:
    unique = list(dict.fromkeys(v for v in (_s(x) for x in values) if v))
    more = len(unique) - MAX_LOCATIONS
    return "; ".join(unique[:MAX_LOCATIONS]) + (f" (+{more} more)" if more > 0 else "")


def _workplace(value: Any) -> str:
    return _WORKPLACE.get(re.sub(r"[^a-z]", "", _s(value).lower()), "")


def _http_url(value: Any) -> str:
    url = _s(value)
    return url if url.startswith(("https://", "http://")) else ""


def _first_time(item: dict[str, Any], fields: tuple[str, ...]) -> tuple[datetime | None, str]:
    """The first parseable timestamp among `fields` (in UTC), and the field it came from."""
    for name in fields:
        value = item.get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            when = parse_time(value)
        except (ValueError, OverflowError, OSError, TypeError):
            when = None
        if when is not None:
            return when.astimezone(timezone.utc), name
    return None, ""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _lever_salary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    low, high = value.get("min"), value.get("max")
    if not isinstance(low, (int, float)) and not isinstance(high, (int, float)):
        return ""
    amount = "-".join(f"{v:,.0f}" for v in (low, high) if isinstance(v, (int, float)))
    return " ".join(x for x in (_s(value.get("currency")), amount, _s(value.get("interval"))) if x)
