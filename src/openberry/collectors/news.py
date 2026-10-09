"""News collectors: Google News RSS search and generic RSS/Atom feeds.

Sources
    GoogleNewsCollector
        GET https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en
        One request per `signals.news_queries` entry (no key, no signup). hl/gl/ceid must agree or
        Google returns nothing. A search feed holds about 100 items, so ` when:<N>d` (N = days in the
        scan window) is appended unless the query already uses when:/after:/before:, otherwise older
        relevance-ranked stories crowd out new ones. Items are RSS 2.0: <title> is "Headline - Publisher",
        <link>/<guid> are Google article ids (news.google.com/rss/articles/CBMi...), not publisher URLs,
        and <source url=...> names the publisher, so the publisher suffix is stripped from the headline.
        The publisher domain is NOT the company's domain and is only kept in `raw`.
    RssCollector
        GET every URL in `signals.rss_feeds` (http/https only), e.g. https://techcrunch.com/feed/
        (~20 latest items, categories such as "Fundraising"). Any RSS 0.9x/1.0/2.0 or Atom feed works.
        Only entries whose title, summary or categories mention one of `signals.keywords`, a competitor,
        or a news query (all its words, Google-style: "quoted phrases", OR, -exclusions) are kept,
        otherwise every article would become a signal.
    Google News is downloaded with the shared httpx client, user-supplied feeds with a client that only
    connects to public addresses (netguard.public_client), and both are parsed from bytes with feedparser
    (feedparser.parse() on a plain string fetches URLs and reads local files, so it is never given one).

What it emits (classified from the headline)
    funding         "Acme raises $18M Series A", "... secures €12 million seed round", "... has raised",
                    "Series B round", "investment led by"                          -> account lead
                    (not raised prices/fares/forecasts/bids/stakes, nor contracts, orders or acquisitions)
    job_change      "Acme appoints Jane Doe as CTO", "Jane Doe joins Acme as VP", "promoted to",
                    "Acme names new CFO" -> person lead (name, title, company) when the headline names
                    the person, otherwise an account lead
    company_news    other news whose headline starts with a company doing something
                    ("Acme opens Dubai office")                                    -> account lead
    keyword_mention RSS only: an article about one of your topics that names no company -> no lead
    The company is read from the headline with deliberately conservative regexes: it must be the
    capitalised subject at the start ("Acme Pay raises", "Dubai-based fintech Ledgerly secures"), or
    follow "joins ... as" / "named CEO of". When no company is found, or it is a competitor or
    yourself, the signal is still stored (it shows in the signal feed) but creates no lead.

Strength (50 = typical)
    funding 70 with an amount or a named round, else 55; job_change 65 for C-level/President/VP/Head
    roles, else 55; company_news 60 for expansion news (opens an office, expands to, enters, relocates),
    else 45; keyword_mention 40. +10 when one of your keywords (RSS: a keyword or news query) is in
    the headline, -10 when an RSS entry matched only in its summary/categories, at most 40 when the
    item names no account or person to act on, and always within 25-85.

Limits and politeness
    Sequential requests with a pause between them; at most MAX_QUERIES_PER_SCAN Google News searches
    and MAX_FEEDS_PER_SCAN feeds per scan (the subset rotates daily when more are configured); a time
    budget plus per-hop timeouts (host lookup included) keep the collector inside the scan timeout;
    feeds over MAX_FEED_BYTES and feed URLs with invalid host names are skipped with a warning.
    Google News is abandoned for the scan on HTTP 403/429/503 (Retry-After is reported) or after 3
    failures in a row; a feed host answering 403/429 is skipped for the rest of the scan and RSS stops
    after 5 failed feeds in a row. Redirects (at most MAX_REDIRECTS) are followed by hand so every
    hop is checked: http(s) only and, unless `Settings.allow_private_feeds` is set, public IP addresses
    only, checked again when connecting so DNS rebinding can't get around it (in public-registration
    mode anyone can submit feed URLs; this stops them probing the server's own network).
    No conditional GET (ETag/If-Modified-Since): nothing is cached between scans.

Terms of use
    Google News RSS is not an official API. The feed's own <copyright> says it is made available
    "solely for the purpose of rendering Google News results within a personal feed reader for
    personal, non-commercial use. Any other use of the feed is expressly prohibited." Treat it as a
    personal / prototype source and prefer publisher feeds (and SEC filings) for commercial use.
    For publisher feeds, only the headline, a short excerpt, the date and the link are stored, with a
    link back to the article; never republish full articles.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import io
import math
import re
import time
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

import feedparser
import httpx

from ..models import Company, LeadIn, SignalIn
from ..netguard import UnsafeURL, assert_public_host, public_client
from .base import CollectContext, Collector, RawSignal, find_terms, parse_time, strip_html, truncate

GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
GOOGLE_NEWS_EDITION = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
FEED_ACCEPT = ("application/rss+xml, application/atom+xml;q=0.9, application/xml;q=0.8, "
               "text/xml;q=0.7, */*;q=0.1")

MAX_QUERIES_PER_SCAN = 10
MAX_FEEDS_PER_SCAN = 15
MAX_FEED_BYTES = 5_000_000
MAX_REDIRECTS = 4
REQUEST_TIMEOUT = 15.0          # per request, seconds (lower of this and Settings.http_timeout)
TIME_BUDGET_SECONDS = 75.0      # no new request after this; services.run_scan cancels at 120 s
MAX_QUERY_CHARS = 300
SUMMARY_LIMIT = 500
TITLE_LIMIT = 160
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
FUNDING_CATEGORIES = frozenset({"fundraising", "funding", "recent funding", "venture capital funding"})

# feedparser "bozo" reasons that do not mean the XML is broken.
_BENIGN_BOZO: tuple[type[BaseException], ...] = (
    feedparser.CharacterEncodingOverride, feedparser.NonXMLContentType, feedparser.UndeclaredNamespace,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------------------


class GoogleNewsCollector(Collector):
    name = "google_news"
    label = "Google News"
    signal_types = ("funding", "company_news", "job_change")
    requires = "news_queries (Google's feed terms allow personal, non-commercial use only)"

    request_interval: float = 1.0                 # seconds between searches (tests set 0)
    time_budget: float = TIME_BUDGET_SECONDS

    def is_configured(self, company: Company) -> bool:
        return any(q.strip() for q in company.signals.news_queries)

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        enabled = set(company.signals.enabled_types)
        since = _aware(ctx.since)
        queries = [q.strip()[:MAX_QUERY_CHARS] for q in company.signals.news_queries if q.strip()]
        if not queries or ctx.max_items <= 0:
            return []
        if len(queries) > MAX_QUERIES_PER_SCAN:
            queries = _rotate(queries, MAX_QUERIES_PER_SCAN, since)
            ctx.warn(f"Google News: {len(company.signals.news_queries)} news queries but at most "
                     f"{MAX_QUERIES_PER_SCAN} are searched per scan; this scan searched: {', '.join(queries)}")
        fetcher = _FeedFetcher(ctx, "Google News", max_requests=MAX_QUERIES_PER_SCAN,
                               interval=self.request_interval, budget=self.time_budget, max_failures=3,
                               block_statuses=frozenset({403, 429, 503}), stop_on_block=True,
                               public_only=False)
        now = _utcnow()
        found = _Found(ctx.max_items)
        for query in queries:
            if fetcher.exhausted or found.full:
                break
            feed = await fetcher.fetch(GOOGLE_NEWS_URL, f"search “{query}”",
                                       params=google_news_params(query, since, now))
            if feed is None:
                continue
            bad: list[str] = []
            for entry in feed.entries:
                try:
                    raw = google_news_signal(entry, query, company, since, now)
                except Exception as exc:  # one odd item must not sink the query
                    bad.append(type(exc).__name__)
                    continue
                if raw is not None and raw.signal.type in enabled:
                    found.add(raw)
            _warn_skipped(ctx, bad, "Google News", ("result", "results"), f"for “{query}”")
        return found.values()


class RssCollector(Collector):
    name = "rss"
    label = "RSS / Atom feeds"
    signal_types = ("funding", "job_change", "company_news", "keyword_mention")
    requires = "rss_feeds (http/https) plus keywords, competitors or news queries to match"

    request_interval: float = 0.5
    time_budget: float = TIME_BUDGET_SECONDS

    def is_configured(self, company: Company) -> bool:
        feeds, _ = split_feed_urls(company.signals.rss_feeds)
        return bool(feeds) and not _Terms.from_company(company).empty

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        enabled = set(company.signals.enabled_types)
        since = _aware(ctx.since)
        feeds, invalid = split_feed_urls(company.signals.rss_feeds)
        for url in invalid:
            ctx.warn(f"RSS: skipped “{_redact(url)}”: not a valid http:// or https:// feed URL")
        terms = _Terms.from_company(company)
        if terms.empty:
            if feeds:
                ctx.warn("RSS: add keywords, competitors or news queries; feed entries are only kept when "
                         "they mention one of them")
            return []
        if not feeds or ctx.max_items <= 0:
            return []
        if len(feeds) > MAX_FEEDS_PER_SCAN:
            total = len(feeds)
            feeds = _rotate(feeds, MAX_FEEDS_PER_SCAN, since)
            ctx.warn(f"RSS: {total} feeds configured but at most {MAX_FEEDS_PER_SCAN} are read per scan; "
                     "the rest rotate in on later scans")
        fetcher = _FeedFetcher(ctx, "RSS", max_requests=MAX_FEEDS_PER_SCAN, interval=self.request_interval,
                               budget=self.time_budget, max_failures=5,
                               block_statuses=frozenset({403, 429}), stop_on_block=False,
                               public_only=not getattr(ctx.settings, "allow_private_feeds", False))
        now = _utcnow()
        found = _Found(ctx.max_items)
        for url in feeds:
            if fetcher.exhausted or found.full:
                break
            feed = await fetcher.fetch(url, f"feed {feed_label(url)}")
            if feed is None:
                continue
            info = FeedInfo(url=url, title=strip_html(feed.feed.get("title")),
                            link=_http_url(feed.feed.get("link")) or url)
            bad: list[str] = []
            for entry in feed.entries:
                try:
                    raw = rss_signal(entry, info, company, terms, since, now)
                except Exception as exc:
                    bad.append(type(exc).__name__)
                    continue
                if raw is not None and raw.signal.type in enabled:
                    found.add(raw)
            _warn_skipped(ctx, bad, "RSS", ("entry", "entries"), f"in {feed_label(url)}")
        return found.values()


def _warn_skipped(ctx: CollectContext, errors: list[str], label: str, noun: tuple[str, str], where: str) -> None:
    """One warning per feed for entries that could not be read, not one per entry."""
    if errors:
        n = len(errors)
        ctx.warn(f"{label}: skipped {n} malformed {noun[n != 1]} {where} ({', '.join(dict.fromkeys(errors))})")


# --------------------------------------------------------------------------------------
# HTTP + parsing
# --------------------------------------------------------------------------------------


class _FetchError(Exception):
    """kind: "fail" (counts towards the failure streak), "block" (host refused us), "skip" (bad URL)."""

    def __init__(self, message: str, kind: str = "fail") -> None:
        super().__init__(message)
        self.kind = kind


def _feed_client(ctx: CollectContext, public_only: bool) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """The shared client, or for public-only feeds one that checks every address it connects to
    (the host check before each request can be dodged by DNS rebinding). Tests swap this out."""
    if not public_only:
        return nullcontext(ctx.client)
    return public_client(headers=ctx.client.headers)


class _FeedFetcher:
    """Sequential, capped feed downloads. Never raises: every problem becomes one ctx warning."""

    def __init__(self, ctx: CollectContext, label: str, *, max_requests: int, interval: float,
                 budget: float, max_failures: int, block_statuses: frozenset[int], stop_on_block: bool,
                 public_only: bool) -> None:
        self.ctx = ctx
        self.label = label
        self.max_requests = max_requests
        self.interval = interval
        self.max_failures = max_failures
        self.block_statuses = block_statuses
        self.stop_on_block = stop_on_block
        self.public_only = public_only
        self.timeout = min(ctx.settings.http_timeout or REQUEST_TIMEOUT, REQUEST_TIMEOUT)
        self.used = 0
        self.failures_in_a_row = 0
        self.stopped = False
        self.blocked_hosts: set[str] = set()
        start = time.monotonic()
        self.deadline = start + budget
        self.hard_deadline = self.deadline + REQUEST_TIMEOUT + 10

    @property
    def exhausted(self) -> bool:
        return self.stopped or self.used >= self.max_requests

    async def fetch(self, url: str, what: str, params: dict[str, str] | None = None) -> Any | None:
        """Download and parse one feed. Returns the feedparser result, or None when it failed."""
        host = (urlsplit(url).hostname or "").lower()
        if self.exhausted or host in self.blocked_hosts:
            return None
        if time.monotonic() >= self.deadline:
            self._stop(f"{self.label}: time budget for this scan used up; skipped the remaining requests")
            return None
        if self.used and self.interval > 0:
            await asyncio.sleep(self.interval)
        self.used += 1
        try:
            body, final_url, content_type = await self._download(url, params)
        except _FetchError as exc:
            message = f"{self.label}: {what} {exc}"
            if exc.kind == "block":
                self._block(host, message)
            elif exc.kind == "skip":
                self.ctx.warn(message)
            else:
                self._fail(message)
            return None
        try:
            parsed, problem = await asyncio.to_thread(parse_feed, body, final_url, content_type)
        except Exception as exc:  # feedparser is tolerant, but never let it sink the scan
            self._fail(f"{self.label}: {what} could not be parsed ({type(exc).__name__})")
            return None
        if problem and not parsed.entries:
            self._fail(f"{self.label}: {what} {problem}")
            return None
        if problem:
            self.ctx.warn(f"{self.label}: {what} {problem}; used the {len(parsed.entries)} items that could be read")
        self.failures_in_a_row = 0
        return parsed

    async def _download(self, url: str, params: dict[str, str] | None) -> tuple[bytes, str, str]:
        current, current_params = url, params
        async with _feed_client(self.ctx, self.public_only) as client:
            for _ in range(MAX_REDIRECTS + 1):
                timeout = self._hop_timeout()
                await self._check_target(current, timeout)
                try:
                    async with client.stream("GET", current, params=current_params,
                                             headers={"Accept": FEED_ACCEPT}, follow_redirects=False,
                                             timeout=httpx.Timeout(timeout)) as resp:
                        location = resp.headers.get("location")
                        if resp.status_code in REDIRECT_STATUSES and location:
                            current, current_params = urljoin(str(resp.url), location.strip()), None
                            continue
                        self._check_status(resp)
                        body = await self._read(resp)
                        return body, str(resp.url), resp.headers.get("content-type", "")
                except _FetchError:
                    raise
                except UnsafeURL as exc:  # refused when connecting: the host now resolves to a non-public address
                    raise _FetchError(f"skipped: {exc} (only public hosts are fetched)", "skip") from exc
                except httpx.TimeoutException as exc:
                    raise _FetchError("timed out") from exc
                except Exception as exc:  # transport errors, invalid URLs, broken streams...
                    raise _FetchError(f"failed ({type(exc).__name__})") from exc
        raise _FetchError(f"redirected more than {MAX_REDIRECTS} times")

    def _hop_timeout(self) -> float:
        """Per-hop timeout, shrunk near the hard deadline so host check + connect + first byte all end by it
        (services.run_scan cancels the whole collector, losing what it found, if it runs past 120 s)."""
        remaining = self.hard_deadline - time.monotonic()
        if remaining < 0.5:
            raise _FetchError("was skipped: time budget for this scan used up")
        return min(self.timeout, remaining / 3)

    async def _check_target(self, url: str, timeout: float) -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise _FetchError("redirected to a non-http(s) URL", "skip")
        if not _valid_hostname(parts.hostname):
            raise _FetchError("skipped: invalid host name", "skip")
        if self.public_only:
            try:
                await asyncio.wait_for(assert_public_host(url), timeout)
            except UnsafeURL as exc:
                raise _FetchError(f"skipped: {exc} (only public hosts are fetched)", "skip") from exc
            except (asyncio.TimeoutError, TimeoutError) as exc:  # a stalled resolver has no timeout of its own
                raise _FetchError("host lookup timed out") from exc
            except Exception as exc:  # e.g. UnicodeError from the resolver's IDNA codec: never sink the scan
                raise _FetchError(f"skipped: cannot check host ({type(exc).__name__})", "skip") from exc

    def _check_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status in self.block_statuses:
            what = "rate limited" if status == 429 else "refused"
            retry = resp.headers.get("retry-after", "").strip()
            hint = f", retry after {retry}s" if retry.isdigit() else ""
            raise _FetchError(f"was {what} (HTTP {status}{hint})", "block")
        if not 200 <= status < 300:
            raise _FetchError(f"returned HTTP {status}")

    async def _read(self, resp: httpx.Response) -> bytes:
        too_big = f"is larger than {MAX_FEED_BYTES // 1_000_000} MB"
        length = resp.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_FEED_BYTES:
            raise _FetchError(too_big)
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise _FetchError(too_big)
            if time.monotonic() > self.hard_deadline:
                raise _FetchError("was too slow to download")
            chunks.append(chunk)
        return b"".join(chunks)

    def _fail(self, message: str) -> None:
        self.ctx.warn(message)
        self.failures_in_a_row += 1
        if self.failures_in_a_row >= self.max_failures and not self.stopped:
            self._stop(f"{self.label}: {self.failures_in_a_row} failed requests in a row; stopped this scan")

    def _block(self, host: str, message: str) -> None:
        if self.stop_on_block:
            self.stopped = True
            self.ctx.warn(f"{message}; skipped the remaining searches this scan")
        else:
            self.blocked_hosts.add(host)
            self.ctx.warn(f"{message}; skipped {host} for the rest of this scan")

    def _stop(self, message: str) -> None:
        self.stopped = True
        self.ctx.warn(message)


def parse_feed(body: bytes, url: str, content_type: str = "") -> tuple[Any, str]:
    """feedparser result plus a short problem description ("" when the feed is fine)."""
    headers = {"content-location": url}
    if content_type:
        headers["content-type"] = content_type
    # Bytes in a stream: feedparser treats a str argument as a URL or file path to open.
    parsed = feedparser.parse(io.BytesIO(body), response_headers=headers)
    exc = parsed.get("bozo_exception") if parsed.get("bozo") else None
    malformed = exc is not None and not isinstance(exc, _BENIGN_BOZO)
    if not parsed.get("version") and not parsed.entries:
        looks_like_feed = re.search(rb"<(?:\?xml|rss|feed|rdf:rdf)\b", body[:1000], re.I) is not None
        return parsed, "returned malformed XML" if malformed and looks_like_feed else "did not return an RSS/Atom feed"
    if malformed:
        return parsed, "returned malformed XML"
    return parsed, ""


def google_news_params(query: str, since: datetime, now: datetime) -> dict[str, str]:
    q = query.strip()
    if not re.search(r"\b(?:when|after|before):", q, re.I):
        # An hour of slack: run_scan computes `since` a moment before the collector reads the clock,
        # and a 14-day lookback should search when:14d, not when:15d.
        days = max(1, math.ceil((now - since).total_seconds() / 86400 - 1 / 24))
        q = f"{q} when:{days}d"
    return {"q": q, **GOOGLE_NEWS_EDITION}


def _aware(since: datetime) -> datetime:
    """ctx.since as aware UTC (a naive value would make every comparison raise)."""
    return parse_time(since) or datetime.min.replace(tzinfo=timezone.utc)


def _valid_hostname(host: str | None) -> bool:
    """Syntax only: 1-63 character labels, at most 253 characters (the resolver raises on anything else)."""
    host = (host or "").rstrip(".")
    if not host or len(host) > 253:
        return False
    return host.startswith("[") or ":" in host or all(0 < len(label) <= 63 for label in host.split("."))


def split_feed_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    """(valid http(s) feed URLs, rejected entries), de-duplicated in order."""
    valid: list[str] = []
    invalid: list[str] = []
    for url in urls:
        url = url.strip()
        try:
            parts = urlsplit(url)
            ok = parts.scheme.lower() in ("http", "https") and _valid_hostname(parts.hostname)
        except ValueError:
            ok = False
        target = valid if ok else invalid
        if url and url not in target:
            target.append(url)
    return valid, invalid


def feed_label(url: str) -> str:
    """host/path of a feed URL for warnings: no credentials, no query string (it may hold API keys)."""
    parts = urlsplit(url)
    return truncate(f"{parts.hostname or ''}{parts.path if parts.path not in ('', '/') else ''}", 80)


def _redact(url: str) -> str:
    """A rejected feed URL for warnings, without credentials, query string or fragment."""
    return truncate(re.sub(r"[?#].*$", "", re.sub(r"(//|^)[^/@\s]*@", r"\1", url.strip(), count=1)), 80)


def _rotate(items: list[str], cap: int, since: datetime) -> list[str]:
    """`cap` items, starting at an offset that moves daily so every item gets its turn."""
    start = since.date().toordinal() % len(items)
    return (items[start:] + items[:start])[:cap]


def _http_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if re.match(r"^https?://[^\s/]+", url, re.I) else ""


def entry_time(entry: Any, now: datetime) -> datetime | None:
    """Published (else updated) time of a feed entry, aware UTC, never in the future."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    when = parse_time(calendar.timegm(struct))
    return min(when, now) if when else None


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# Google News items
# --------------------------------------------------------------------------------------


def google_news_signal(entry: Any, query: str, company: Company, since: datetime,
                       now: datetime) -> RawSignal | None:
    occurred = entry_time(entry, now)
    if occurred is None or occurred <= since:
        return None
    source = entry.get("source") or {}
    publisher = strip_html(source.get("title"))
    headline, publisher = split_publisher(strip_html(entry.get("title")), publisher)
    key = str(entry.get("id") or entry.get("link") or "").strip()
    if not headline or not key:
        return None

    info = analyze_headline(headline)
    kind = info.kind or "company_news"
    account, competitor = vet_account(info.account, company)
    lead = _person_lead(info, account, "google_news")
    topic_in_title = any(_term_re(k).search(headline) for k in company.signals.keywords if len(k.strip()) >= 2)
    strength = news_strength(kind, info, has_target=bool(account), topic_in_title=topic_in_title)
    summary = f"{publisher}: {headline}" if publisher else headline
    raw = {
        "query": query,
        "queries": [query],
        "publisher": publisher,
        "publisher_url": _http_url(source.get("href")),
        "guid": key,
        "account": account,
        "person": lead.full_name if lead else "",
        "role": info.role,
        "amount": info.amount,
        "round": info.round,
        "competitor": competitor,
    }
    signal = SignalIn(
        type=kind,
        title=truncate(headline, TITLE_LIMIT),
        summary=truncate(f"{summary}{'' if summary.endswith(('.', '?', '!')) else '.'} "
                         f"Found by the Google News search “{query}”.", SUMMARY_LIMIT),
        url=_http_url(entry.get("link")) or f"https://news.google.com/search?q={quote_plus(query)}",
        source="google_news",
        external_id=f"gn:{_hash(key)}",
        strength=strength,
        occurred_at=occurred,
        raw={k: v for k, v in raw.items() if v not in ("", None, [])},
    )
    return RawSignal(signal=signal, lead=lead, account=account)


def split_publisher(title: str, publisher: str) -> tuple[str, str]:
    """'Headline - Publisher' -> ('Headline', 'Publisher'); <source> wins when it agrees."""
    title = title.strip()
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(publisher) - 3].strip(), publisher
    head, sep, tail = title.rpartition(" - ")
    if sep and head.strip() and tail.strip() and len(tail) <= 60 and len(tail.split()) <= 6:
        return head.strip(), publisher or tail.strip()
    return title, publisher


# --------------------------------------------------------------------------------------
# RSS / Atom entries
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedInfo:
    url: str
    title: str
    link: str


@dataclass(frozen=True)
class QueryMatcher:
    """A Google-style query matched against text: every group needs one of its alternatives."""

    query: str
    groups: tuple[tuple[str, ...], ...]
    excluded: tuple[str, ...] = ()

    @classmethod
    def parse(cls, query: str) -> QueryMatcher:
        groups: list[list[str]] = []
        excluded: list[str] = []
        join_next = False
        for token in re.findall(r'-?"[^"]*"|\S+', query):
            if token.upper() in ("OR", "|"):
                join_next = bool(groups)
                continue
            negative = token.startswith("-") and len(token) > 1
            token = token[1:] if negative else token
            op, colon, value = token.partition(":")
            if colon and not op.startswith('"'):
                if op.lower() not in ("intitle", "allintitle", "intext", "allintext"):
                    join_next = False
                    continue  # when:, site:, source:, after:, before:... only make sense to Google
                token = value
            term = token.strip('"()').strip()
            if len(term) < 2:
                join_next = False
                continue
            if negative:
                excluded.append(term)
            elif join_next:
                groups[-1].append(term)
            else:
                groups.append([term])
            join_next = False
        return cls(query=query, groups=tuple(tuple(g) for g in groups), excluded=tuple(excluded))

    def matches(self, text: str) -> bool:
        if not self.groups or any(_term_re(t).search(text) for t in self.excluded):
            return False
        return all(any(_term_re(t).search(text) for t in group) for group in self.groups)


@dataclass(frozen=True)
class _Terms:
    keywords: tuple[str, ...]
    competitors: tuple[str, ...]
    queries: tuple[QueryMatcher, ...]

    @classmethod
    def from_company(cls, company: Company) -> _Terms:
        queries = tuple(m for m in (QueryMatcher.parse(q) for q in company.signals.news_queries) if m.groups)
        return cls(keywords=tuple(t for t in company.signals.keywords if len(t.strip()) >= 2),
                   competitors=tuple(t for t in company.competitors if len(t.strip()) >= 2),
                   queries=queries)

    @property
    def empty(self) -> bool:
        return not (self.keywords or self.competitors or self.queries)

    def topics(self, text: str) -> list[str]:
        """Keywords and news queries found in `text` (your topics, not competitors)."""
        hits = [t for t in self.keywords if _term_re(t).search(text)]
        return hits + [q.query for q in self.queries if q.matches(text)]

    def competitors_in(self, text: str) -> list[str]:
        return [c for c in self.competitors if _term_re(c).search(text)]


def rss_signal(entry: Any, feed: FeedInfo, company: Company, terms: _Terms, since: datetime,
               now: datetime) -> RawSignal | None:
    occurred = entry_time(entry, now)
    if occurred is None or occurred <= since:
        return None
    title = strip_html(entry.get("title"))
    if not title:
        return None
    summary = strip_html(_entry_summary(entry))
    categories = list(dict.fromkeys(
        c for c in (strip_html(t.get("term") or t.get("label")) for t in entry.get("tags") or []) if c))[:10]
    body = " ".join([title, summary, *categories])

    title_topics = terms.topics(title)
    topics = list(dict.fromkeys(title_topics + terms.topics(body)))
    competitors = terms.competitors_in(body)
    if not topics and not competitors:
        return None
    in_title = bool(title_topics or terms.competitors_in(title))

    info = analyze_headline(title)
    kind = info.kind
    if not kind and any(c.lower() in FUNDING_CATEGORIES for c in categories):
        kind = "funding"
    account, competitor = vet_account(info.account, company)
    if not kind:
        kind = "company_news" if (account or competitor) else "keyword_mention"
    lead = _person_lead(info, account, "rss")
    strength = news_strength(kind, info, has_target=bool(account), topic_in_title=bool(title_topics),
                             summary_only=not in_title)
    author = strip_html(entry.get("author"))
    raw = {
        "feed": feed.url,
        "feed_title": feed.title,
        "matched": topics + [c for c in competitors if c not in topics],
        "matched_in": "title" if in_title else "summary",
        "categories": categories[:5],
        "author": author,
        "account": account,
        "person": lead.full_name if lead else "",
        "role": info.role,
        "amount": info.amount,
        "round": info.round,
        "competitor": competitor or (competitors[0] if competitors else ""),
    }
    signal = SignalIn(
        type=kind,
        title=truncate(title, TITLE_LIMIT),
        summary=truncate(summary or title, SUMMARY_LIMIT),
        url=_http_url(entry.get("link")) or feed.link,
        source="rss",
        external_id=f"rss:{_hash(_entry_key(entry, feed.url))}",
        strength=strength,
        occurred_at=occurred,
        raw={k: v for k, v in raw.items() if v not in ("", None, [])},
    )
    return RawSignal(signal=signal, lead=lead, account=account)


def _entry_summary(entry: Any) -> str:
    if entry.get("summary"):
        return str(entry["summary"])
    for content in entry.get("content") or []:
        if content.get("value"):
            return str(content["value"])
    return ""


def _entry_key(entry: Any, feed_url: str) -> str:
    """Feed-independent identity of an entry (the same article in two feeds is one signal)."""
    ident = str(entry.get("id") or "").strip()
    if ident and not re.match(r"^[a-z][a-z0-9+.-]*:", ident, re.I):
        ident = f"{urlsplit(feed_url).hostname}|{ident}"  # bare guid like "12345": unique per site only
    return ident or str(entry.get("link") or "").strip() or \
        f"{feed_url}|{entry.get('title', '')}|{entry.get('published') or entry.get('updated') or ''}"


# --------------------------------------------------------------------------------------
# Headline analysis (pure functions)
# --------------------------------------------------------------------------------------

_MONEY = (r"(?:[$€£¥₹]\s?\d[\d,.]*(?:\s?(?:k|m|mn|mm|million|b|bn|billion)\b)?"
          r"|\b(?:usd|us\$|eur|gbp|aed|sar|inr|sgd|chf|cad|aud)\s?\d[\d,.]*(?:\s?(?:k|m|mn|million|b|bn|billion)\b)?"
          r"|\b\d[\d,.]*\s?(?:million|billion|mn|bn)\b)")
_FUNDING_OBJECT = (rf"(?:{_MONEY}|\bseries\s+[a-f]\d?\b|\b(?:pre-?)?seed\b"
                   r"|\b(?:funding|financing|investment|extension)\s+round\b|\bventure\s+debt\b)")
_RAISE_VERBS = r"(?:rais(?:es|ed|ing)|raise)"
# What else gets "raised": prices, fares, forecasts, bids, stakes... ("Uber raises minimum fare to AED 12").
_NOT_RAISED = (r"(?:prices?|pricing|fares?|rates?|fees?|tariffs?|tolls?|rents?|salar(?:y|ies)|wages?|"
               r"pay\s+(?:to|for|by)|"
               r"forecasts?|guidance|outlook|targets?|estimates?|expectations?|projections?|dividends?|payouts?|"
               r"bids?|offers?|stakes?|holdings?|caps?|limits?|ceilings?|minimums?|prizes?|capacity|production|"
               r"output|awareness|concerns?|questions?|alarms?|eyebrows|hopes?|fears?|doubts?)")
_NOT_RAISED_RE = re.compile(rf"\b{_NOT_RAISED}\b", re.I)
# Verbs also used for contracts, orders and acquisitions: only funding when no deal word is around.
_DEAL_VERBS = (r"(?:secur(?:es|ed)|secure|clos(?:es|ed)|close|land(?:s|ed)?|bag(?:s|ged)?|net(?:s|ted)?|"
               r"snag(?:s|ged)?|scor(?:es|ed)|score|pick(?:s|ed)?\s+up|nab(?:s|bed)?|rake(?:s|d)?\s+in|"
               r"attract(?:s|ed)?|receiv(?:es|ed)|receive)")
_FUNDING_RAISE_RE = re.compile(rf"\b{_RAISE_VERBS}\b(?P<gap>[^.;:|]{{0,40}}?){_FUNDING_OBJECT}", re.I)
_FUNDING_VERB_RES = (
    re.compile(r"\b(?:announc(?:es|ed)|announce|complet(?:es|ed)|complete|unveil(?:s|ed)?)\s+"
               rf"(?:its\s+|a\s+|an\s+|the\s+)?(?:{_MONEY}\s+)?(?:series\s+[a-f]\d?|(?:pre-?)?seed|funding|financing)\b",
               re.I),
    re.compile(rf"\bha(?:s|ve)\s+(?:now\s+|just\s+)?raised\b(?!\s+(?:its\s+|their\s+|the\s+)?{_NOT_RAISED}\b)", re.I),
)
_FUNDING_DEAL_RE = re.compile(rf"\b{_DEAL_VERBS}\b[^.;:|]{{0,40}}?{_FUNDING_OBJECT}", re.I)
_DEAL_WORDS_RE = re.compile(r"\b(?:acqui\w*|merger|buyout|takeover|contracts?|deals?|orders?|sale|tender|"
                            r"partnership|award)\b", re.I)
_ROUND_NAMED_RE = re.compile(r"\bseries\s+[a-f]\d?\b|\b(?:pre-?)?seed\b|\b(?:funding|financing|investment)\s+round\b",
                             re.I)
_FUNDING_PLAIN_RES = (
    re.compile(r"\bseries\s+[a-f]\d?\s+(?:round|funding|financing|raise|investment|extension)\b", re.I),
    re.compile(r"\b(?:pre-?)?seed\s+(?:round|funding|financing|investment|raise)\b", re.I),
    re.compile(r"\bfunding\s+round\b", re.I),
    re.compile(r"\b(?:investment|round|funding|financing)\s+(?:was\s+)?led\s+by\b", re.I),
)

_ROLE = (r"(?:c[etfomirp]o|ciso|chief\s+[\w-]+(?:\s+[\w-]+)?\s+officer|chief|president|chair(?:man|woman|person)?|"
         r"head(?!\s*-?\s*(?:count|quarters?|lines?))|vp|svp|evp|vice\s+president|managing\s+director|director|"
         r"general\s+manager|country\s+manager)\b")
_LEADIN = r"(?:(?:its|the|their|a|an|new|first|next|interim|acting|permanent|global|group|regional|senior|executive)\s+)*"
_APPOINT_VERBS = (r"(?:appoint(?:s|ed)?|appointments?|nam(?:es|ed)|name|hir(?:es|ed)|hire|tap(?:s|ped)?|"
                  r"promot(?:es|ed)|promote|welcom(?:es|ed)|welcome|elevat(?:es|ed)|elevate)")
_JOB_RES = (
    re.compile(rf"\b{_APPOINT_VERBS}\b(?:\s+[^\s,;:]+){{0,6}}?\s*,?\s+(?:as\s+|to\s+(?:the\s+role\s+of\s+)?)?"
               rf"{_LEADIN}(?P<role>{_ROLE})", re.I),
    re.compile(rf"\bjoin(?:s|ed)?\b(?:\s+[^\s,;:]+){{1,6}}?\s+as\s+{_LEADIN}(?P<role>{_ROLE})", re.I),
    re.compile(rf"\b(?:promoted|elevated)\s+to\s+{_LEADIN}(?P<role>{_ROLE})", re.I),
)
_ROLE_END_RE = re.compile(r"\s*(?:[,;:|(]|\s[-–—]\s)|\s+(?:amid|ahead|after|as|at|from|following|with|while|"
                          r"during|in|on|to|for|effective|replacing|succeeding)\s", re.I)
_SENIOR_RE = re.compile(r"\b(?:c[etfomirp]o|ciso|chief|president|chair\w*|vp|svp|evp|vice\s+president|head|"
                        r"managing\s+director|general\s+manager|founder)\b", re.I)
# "Acme enters administration", "Acme expands layoffs": not growth news.
_DISTRESS_RE = re.compile(r"\b(?:administration|liquidation|bankrupt\w*|insolven\w*|receivership|layoffs?|lays?\s+off|"
                          r"job\s+cuts|shut(?:s|ting)?\s+down|winds?\s+down|probe|investigation|lawsuit)\b", re.I)
_EXPANSION_RE = re.compile(
    r"\b(?:open(?:s|ed|ing)?(?![-\s]?source)|expand(?:s|ed|ing)?|expansion|enter(?:s|ed|ing)?|launch(?:es|ed)?\s+in|"
    r"relocat(?:es|ed|ing|ion)|relocate|mov(?:es|ed|ing)\s+(?:its\s+)?(?:hq|headquarters)|sets?\s+up|"
    r"establish(?:es|ed)?|inaugurat(?:es|ed))\b|\bnew\s+(?:office|offices|hq|headquarters|hub|base)\b", re.I)

# Company names: capitalised words ("Acme Pay", "B2B Labs", "eToro"), joined by &/of/de at most.
_CO_WORD = r"(?:[A-Z0-9À-ÖØ-Þ][\w&.'’+\-]*|[a-z]{1,3}[A-Z][\w&.'’+\-]*)"
_CO = rf"(?P<co>{_CO_WORD}(?:\s+(?:{_CO_WORD}|&|of|de|du|la|del)){{0,5}}?)"
_CO_TAIL = rf"(?P<co>{_CO_WORD}(?:\s+(?:{_CO_WORD}|&|of|de|du|la|del)){{0,5}})"   # greedy, at the end
_PREFIX = r"^(?:(?i:exclusive|breaking|report|reports|scoop|update|funding|deal|news|watch|just\s+in)\s*[:|–—-]\s*)?"
_DESCRIPTOR_WORDS = (r"startup|start-up|company|firm|platform|unicorn|scale-?up|provider|maker|developer|marketplace|"
                     r"operator|specialist|rival|challenger|fintech|insurtech|proptech|healthtech|edtech|legaltech|"
                     r"[\w-]+-based|[\w-]+-backed")
_DESCRIPTOR = rf"(?:(?:[\w'’.$€£\-]+\s+){{0,4}}?(?:{_DESCRIPTOR_WORDS})\s+)?"
_AUX = r"(?:(?i:has|have|will|to|plans\s+to|set\s+to|is\s+set\s+to|reportedly|officially|now|also)\s+)?"
_ACCOUNT_VERBS = (
    r"(?i:rais(?:es|ed)|raise|secur(?:es|ed)|secure|clos(?:es|ed)|close|land(?:s|ed)?|bag(?:s|ged)?|net(?:s|ted)|"
    r"snag(?:s|ged)?|scor(?:es|ed)|pick(?:s|ed)?\s+up|attract(?:s|ed)?|receiv(?:es|ed)|announc(?:es|ed)|"
    r"complet(?:es|ed)|appoint(?:s|ed)?|nam(?:es|ed)|hir(?:es|ed)|hire|tap(?:s|ped)|promot(?:es|ed)|welcom(?:es|ed)|"
    r"elevat(?:es|ed)|open(?:s|ed)?|launch(?:es|ed)?|expand(?:s|ed)?|enter(?:s|ed)?|acquir(?:es|ed)|acquire|"
    r"buys?|bought|unveil(?:s|ed)?|debut(?:s|ed)?|relocat(?:es|ed)|relocate|mov(?:es|ed)|partner(?:s|ed)?|"
    r"sign(?:s|ed)?|select(?:s|ed)?|chooses|chose|plan(?:s|ned)?|invest(?:s|ed)?|sets?\s+up|establish(?:es|ed)?|"
    r"inaugurat(?:es|ed)|roll(?:s|ed)?\s+out|add(?:s|ed)|cuts?|lays?\s+off|wins?|won)"
)
_ACCOUNT_RE = re.compile(rf"{_PREFIX}{_DESCRIPTOR}{_CO},?\s+{_AUX}{_ACCOUNT_VERBS}\b")

_NAME_WORD = r"[A-ZÀ-ÖØ-Þ][\w'’.\-]*"
_NAME_SEQ = (rf"{_NAME_WORD}(?:\s+(?:(?:al|el|bin|bint|ibn|de|del|della|da|di|du|van|von|der|den|la|le)\s+)?"
             rf"{_NAME_WORD}){{1,3}}?")
_PERSON_ROLE_RE = re.compile(rf"(?<![\w'’])(?P<person>{_NAME_SEQ})\s*,?\s+(?:(?i:as|to)\s+)?(?i:{_LEADIN}{_ROLE})")
_JOINS_RE = re.compile(rf"(?<![\w'’])(?P<person>{_NAME_SEQ})\s+(?i:join(?:s|ed)?)\s+{_CO}\s+(?i:as)\s")
_PASSIVE_RE = re.compile(
    rf"^(?P<person>{_NAME_SEQ})\s+(?:(?i:is|has\s+been|was)\s+)?(?i:named|appointed|tapped|picked|hired|promoted|"
    rf"elevated)\s+(?:(?i:as|to)\s+)?(?i:{_LEADIN}{_ROLE})(?:[^,;:]*?)\s(?i:of|at)\s+{_CO_TAIL}(?=$|[,;:.]|\s)")

_BAD_FIRST = frozenset("""
    the a an this that these those why how what when where who which whose is are was were will can could
    should would may might do does did it its he she they we you i our his her their here there report
    exclusive breaking update opinion analysis podcast video live watch inside meet top best new more most
    after before as in on at for with from by if amid and or but not no every all some many several
    investors startups companies firms founders banks vcs sources study survey data letter week today
""".split())
_FUNCTION_WORDS = frozenset("""
    as in to for with on at by after amid from over into is are was were will can may could would should
    why how what who this that it its the a an his her their our says said while than then but or if
""".split())
# Subjects that are not a company (compared lower-case, dots removed; one entry per line may hold spaces).
_GENERIC_ACCOUNTS = frozenset(line.strip() for line in """
    startup
    startups
    company
    companies
    firm
    fintech
    insurtech
    proptech
    platform
    unicorn
    investors
    report
    government
    ministry
    news
    google news
    dubai
    uae
    abu dhabi
    sharjah
    saudi
    saudi arabia
    ksa
    riyadh
    jeddah
    qatar
    doha
    kuwait
    bahrain
    oman
    egypt
    gcc
    gulf
    mena
    middle east
    india
    china
    japan
    singapore
    hong kong
    europe
    eu
    uk
    britain
    us
    usa
    america
    united states
    united kingdom
    germany
    france
    london
    new york
""".splitlines() if line.strip())
# Leading words that describe the subject in a Title Case headline ("Startup Acme Raises $5M").
_LEADING_DESCRIPTORS = frozenset("""
    startup start-up scaleup scale-up unicorn fintech insurtech proptech healthtech edtech legaltech
""".split())
_DESCRIPTOR_TOKEN_RE = re.compile(rf"^(?:{_DESCRIPTOR_WORDS})$", re.I)
_NOT_NAME = frozenset("""
    former new ex exec executive executives veteran industry its the chief head senior global group board
    interim acting president director officer leader expert partner founder cofounder co-founder banker
    lawyer team top key two three four five ceo cto cfo coo cmo cro cio vp svp evp as to and of for with
    investor analyst insider alum alumnus manager staff hire hires appointment appointments announces
""".split())


@dataclass
class Headline:
    kind: str = ""          # "funding", "job_change" or "" (other news)
    account: str = ""
    person: str = ""
    role: str = ""
    amount: str = ""
    round: str = ""
    expansion: bool = False

    @property
    def senior(self) -> bool:
        return bool(_SENIOR_RE.search(self.role))


def analyze_headline(title: str) -> Headline:
    """Classify a news headline and pull out the company (and appointed person) it is about."""
    text = re.sub(r"\s+", " ", (title or "").replace("‘", "'").replace("’", "'")).strip()
    kind, role = _classify(text)
    info = Headline(kind=kind, role=role,
                    expansion=bool(_EXPANSION_RE.search(text)) and not _DISTRESS_RE.search(text))
    if kind == "funding":
        money = re.search(_MONEY, text, re.I)
        info.amount = money.group(0).strip().rstrip(".,") if money else ""
        info.round = _round_name(text)
    if kind == "job_change":
        info.person, info.account = _job_parties(text)
        if info.account:  # "named CEO of Acme": the role is "CEO"
            info.role = re.sub(rf"\s+(?:of|at)\s+{re.escape(info.account)}$", "", info.role, flags=re.I)
        if info.person and info.person.lower() in info.role.lower():
            info.role = _new_role(text, info.person, info.role)
    if not info.account:
        info.account = _account_from_subject(text)
    return info


def _classify(text: str) -> tuple[str, str]:
    """(kind, role). The verb that comes first wins when a headline matches both kinds."""
    candidates: list[tuple[int, str, str]] = []
    for m in _FUNDING_RAISE_RE.finditer(text):
        if not _NOT_RAISED_RE.search(m.group("gap")):
            candidates.append((m.start(), "funding", ""))
            break
    for regex in _FUNDING_VERB_RES:
        if m := regex.search(text):
            candidates.append((m.start(), "funding", ""))
    if (m := _FUNDING_DEAL_RE.search(text)) and (_ROUND_NAMED_RE.search(text) or not _DEAL_WORDS_RE.search(text)):
        candidates.append((m.start(), "funding", ""))
    if any(regex.search(text) for regex in _FUNDING_PLAIN_RES):
        candidates.append((len(text), "funding", ""))  # no verb: lowest priority
    for regex in _JOB_RES:
        if m := regex.search(text):
            candidates.append((m.start(), "job_change", _role_text(text, m.start("role"))))
    if not candidates:
        return "", ""
    _, kind, role = min(candidates, key=lambda c: (c[0], c[1] != "job_change"))
    return kind, role


def _role_text(text: str, start: int) -> str:
    tail = text[start:]
    end = _ROLE_END_RE.search(tail)
    role = (tail[: end.start()] if end else tail).strip(" .,'\"")
    return truncate(role, 80)


def _round_name(text: str) -> str:
    if m := re.search(r"\bseries\s+([a-f])(\d?)\b", text, re.I):
        return f"Series {m.group(1).upper()}{m.group(2)}"
    if re.search(r"\bpre-?seed\b", text, re.I):
        return "Pre-seed"
    return "Seed" if re.search(r"\bseed\b", text, re.I) else ""


def _account_from_subject(text: str) -> str:
    m = _ACCOUNT_RE.match(text)
    return clean_account(m.group("co")) if m else ""


def _job_parties(text: str) -> tuple[str, str]:
    """(person, company) of an appointment headline; either may be ""."""
    if m := _PASSIVE_RE.search(text):           # "Jane Doe named CEO of Acme"
        return _plausible_person(m.group("person")), clean_account(m.group("co"))
    if m := _JOINS_RE.search(text):             # "Jane Doe joins Acme as CTO"
        return _plausible_person(m.group("person")), clean_account(m.group("co"))
    m = _ACCOUNT_RE.match(text)                 # "Acme appoints Jane Doe as CTO"
    account = clean_account(m.group("co")) if m else ""
    start = m.end() if m else 0
    person = ""
    if p := _PERSON_ROLE_RE.search(text, start):
        person = _plausible_person(p.group("person"))
        if account and person and (person.lower() in account.lower() or account.lower() in person.lower()):
            person = ""
    return person, account


def _plausible_person(candidate: str) -> str:
    """The longest trailing run of 2-4 name-like words ("Former Google Exec Jane Doe" -> "Jane Doe")."""
    tokens = candidate.split()
    for i in range(len(tokens) - 1):
        name = tokens[i:]
        words = [t for t in name if t[:1].isupper()]
        if 2 <= len(words) <= 4 and all(_name_token(t) for t in words):
            return " ".join(name)
    return ""


def _new_role(text: str, person: str, role: str) -> str:
    """"Acme promotes CFO Jane Doe to CEO": the new role follows the person, the old one precedes them."""
    if m := re.search(rf"{re.escape(person)}\s*,?\s+(?:as|to)\s+(?:the\s+role\s+of\s+)?{_LEADIN}(?P<role>{_ROLE})",
                      text, re.I):
        return _role_text(text, m.start("role"))
    return re.sub(r"\s+", " ", re.sub(re.escape(person), " ", role, flags=re.I)).strip(" ,")


def _name_token(token: str) -> bool:
    core = token.strip(".'’")
    if not core or any(ch.isdigit() for ch in core) or core.lower() in _NOT_NAME:
        return False
    if re.search(r"['’]s$", token):  # "Google's Jane Doe": the possessive is the employer, not the name
        return False
    return not (len(core) >= 3 and core.isupper())


def clean_account(name: str) -> str:
    """A plausible company name from a regex capture, or "" when it looks generic or wrong."""
    name = re.sub(r"(?:'s|’s|')$", "", (name or "").strip()).strip(" .,:;-")
    tokens = name.split()
    # "Fintech Firm Ledgerly" / "Startup Acme" (Title Case headline): drop descriptor words in front.
    cut = 0
    for i, token in enumerate(tokens):
        if (_DESCRIPTOR_TOKEN_RE.match(token) and (i > 0 or token.lower().endswith(("-based", "-backed")))) or \
                (i == cut and i < len(tokens) - 1 and token.lower() in _LEADING_DESCRIPTORS):
            cut = i + 1
    tokens = tokens[cut:]
    # "India's Zepto", "Abu Dhabi's ADQ": a place in the possessive is not part of the name.
    for i, token in enumerate(tokens[:-1]):
        if token.endswith(("'s", "’s")) and _is_generic(" ".join(tokens[:i] + [token[:-2]])):
            tokens = tokens[i + 1:]
            break
    if not tokens or len(tokens) > 6:
        return ""
    if tokens[0].lower() in _BAD_FIRST or any(t.lower() in _FUNCTION_WORDS for t in tokens[1:]):
        return ""
    name = " ".join(tokens)
    if _is_generic(name) or not 2 <= len(name) <= 60:
        return ""
    return name


def _is_generic(name: str) -> bool:
    return re.sub(r"\s+", " ", name.lower().replace(".", "")).strip() in _GENERIC_ACCOUNTS


def vet_account(name: str, company: Company) -> tuple[str, str]:
    """(account to attach, competitor it matched). Competitors and yourself never become leads."""
    if not name:
        return "", ""
    key = _norm(name)
    for competitor in company.competitors:
        if _norm(competitor) == key or find_terms(name, [competitor]) or find_terms(competitor, [name]):
            return "", competitor
    if key == _norm(company.name) or find_terms(name, [company.name]):
        return "", ""
    return name, ""


def news_strength(kind: str, info: Headline, *, has_target: bool, topic_in_title: bool = False,
                  summary_only: bool = False) -> int:
    if kind == "funding":
        strength = 70 if (info.amount or info.round) else 55
    elif kind == "job_change":
        strength = 65 if info.senior else 55
    elif kind == "company_news":
        strength = 60 if info.expansion else 45
    else:
        strength = 40
    if topic_in_title:
        strength += 10
    if summary_only:
        strength -= 10
    if not has_target:
        strength = min(strength, 40)  # nobody to act on: weak, informational
    return max(25, min(85, strength))


def _person_lead(info: Headline, account: str, source: str) -> LeadIn | None:
    if info.kind != "job_change" or not info.person or not account:
        return None
    return LeadIn(full_name=info.person, title=info.role, lead_company=account, source=source)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _term_re(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive match that tolerates plurals and hyphen/space variants."""
    words = [re.escape(w) for w in term.lower().split()]
    body = r"[\s\-]+".join(words) or re.escape(term)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?:'?s|es)?(?![a-z0-9])", re.I)


# --------------------------------------------------------------------------------------
# De-duplication within a scan
# --------------------------------------------------------------------------------------


@dataclass
class _Found:
    """Signals of one scan, keyed by external id; syndicated copies of one story collapse into one."""

    limit: int
    by_id: dict[str, RawSignal] = field(default_factory=dict)
    by_story: dict[str, str] = field(default_factory=dict)

    @property
    def full(self) -> bool:
        return len(self.by_id) >= self.limit

    def values(self) -> list[RawSignal]:
        return list(self.by_id.values())

    def add(self, raw: RawSignal) -> None:
        eid = raw.signal.external_id
        story = f"{raw.signal.source}|{_norm(raw.signal.title)}"
        if eid in self.by_id:
            self.by_id[eid] = _merge(self.by_id[eid], raw)
            return
        other_id = self.by_story.get(story)
        if other_id is not None:
            # Same headline from another publisher/feed: keep the earliest copy (stable across scans).
            other = self.by_id.pop(other_id)
            keep, drop = sorted((other, raw), key=lambda r: (r.signal.occurred_at, r.signal.external_id))
            merged = _merge(keep, drop)
            self.by_id[keep.signal.external_id] = merged
            self.by_story[story] = keep.signal.external_id
            return
        if self.full:
            return
        self.by_id[eid] = raw
        self.by_story[story] = eid


def _merge(keep: RawSignal, other: RawSignal) -> RawSignal:
    raw = dict(keep.signal.raw)
    for key in ("queries", "matched"):
        values = list(dict.fromkeys([*raw.get(key, []), *other.signal.raw.get(key, [])]))
        if values:
            raw[key] = values
    if other.signal.external_id != keep.signal.external_id:
        source = other.signal.raw.get("publisher") or other.signal.raw.get("feed_title") or other.signal.raw.get("feed")
        also = [*raw.get("also_in", []), *([source] if source else []), *other.signal.raw.get("also_in", [])]
        also = [a for a in dict.fromkeys(also) if a and a != (raw.get("publisher") or raw.get("feed_title"))]
        if also:
            raw["also_in"] = also[:10]
    signal = keep.signal.model_copy(update={"strength": max(keep.signal.strength, other.signal.strength), "raw": raw})
    return RawSignal(signal=signal, lead=keep.lead, account=keep.account, account_domain=keep.account_domain)
