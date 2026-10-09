"""News collectors (Google News RSS search + generic RSS/Atom feeds), fully offline via httpx.MockTransport.

Fixtures in tests/fixtures/news mirror the payloads from the API research: Google News search feeds
(NFE/5.0 RSS, "Headline - Publisher" titles, Google article-id links/guids, <source url>), a TechCrunch
WordPress feed (dc:creator, categories such as "Fundraising", guid https://techcrunch.com/?p=<id>),
an Atom blog with relative links, a malformed feed and an HTML block page.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from openberry import netguard, repo
from openberry.collectors import COLLECTORS
from openberry.collectors import news
from openberry.collectors.base import CollectContext, RawSignal
from openberry.collectors.news import (
    GoogleNewsCollector,
    QueryMatcher,
    RssCollector,
    analyze_headline,
    news_strength,
    parse_feed,
    split_publisher,
)
from openberry.config import get_settings
from openberry.models import Company, LeadIn
from openberry.services import ingest
from openberry.website import UnsafeURL

FIXTURES = Path(__file__).parent / "fixtures" / "news"
SINCE = datetime(2026, 9, 24, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
GN = "https://news.google.com/rss/search"
TC_FEED = "https://techcrunch.com/feed/"
BLOG_FEED = "https://blog.example.org/feed.atom"
CONF_FEED = "https://feeds.example.com/news.xml"   # the conftest company's feed
PRIVATE_HOSTS = {"intranet.local", "10.0.0.5", "169.254.169.254", "localhost"}
REAL_FEED_CLIENT = news._feed_client  # the autouse fixture below swaps it for the mock client

SERIES_A = "Series A fintech"
DUBAI = "Dubai office opening"
GN_ROUTES = {SERIES_A: "google_news_series_a.xml", DUBAI: "google_news_dubai_office.xml"}
FEED_ROUTES = {TC_FEED: "rss_techcrunch.xml", BLOG_FEED: "atom_blog.xml", CONF_FEED: "rss_techcrunch.xml"}
EMPTY_GN = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><generator>NFE/5.0</generator>'
            b"<title>empty - Google News</title><link>https://news.google.com/</link></channel></rss>")

Override = httpx.Response | Callable[[httpx.Request], httpx.Response]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def xml(name_or_body: str | bytes, status: int = 200, **headers: str) -> httpx.Response:
    body = name_or_body if isinstance(name_or_body, bytes) else fixture_bytes(name_or_body)
    return httpx.Response(status, content=body,
                          headers={"content-type": "application/rss+xml; charset=UTF-8", **headers})


def html(status: int, name: str = "forbidden.html", **headers: str) -> httpx.Response:
    return httpx.Response(status, content=fixture_bytes(name), headers={"content-type": "text/html", **headers})


def gn_id(guid: str) -> str:
    return "gn:" + hashlib.sha1(guid.encode()).hexdigest()[:16]


def rss_id(key: str) -> str:
    return "rss:" + hashlib.sha1(key.encode()).hexdigest()[:16]


class FakeWeb:
    """MockTransport handler: Google News by query, feeds by URL; records requests, injects failures."""

    def __init__(self, gn: dict[str, Override] | None = None, feeds: dict[str, Override] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.gn = gn or {}
        self.feeds = feeds or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        base = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        if base == GN:
            query = re.sub(r"\s+when:\d+d$", "", request.url.params.get("q", ""))
            target: Override | str | None = self.gn.get(query, GN_ROUTES.get(query))
            if target is None:
                return xml(EMPTY_GN)
        else:
            url = str(request.url)
            target = self.feeds.get(url, FEED_ROUTES.get(url))
            if target is None:
                return httpx.Response(404, text="not found")
        if isinstance(target, str):
            return xml(target)
        return target(request) if callable(target) else target

    @property
    def queries(self) -> list[str]:
        return [r.url.params.get("q", "") for r in self.requests if str(r.url).startswith(GN)]

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


@pytest.fixture(autouse=True)
def frozen_clock_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze 'now' and replace DNS-based public-host checks (the sandbox has no network).

    Public-only feeds normally get their own connect-time-checked client (netguard); here every
    request goes through the test's mock client instead."""
    monkeypatch.setattr(news, "_utcnow", lambda: NOW)

    async def fake_assert_public_host(url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host in PRIVATE_HOSTS:
            raise UnsafeURL(f"{host} resolves to a non-public address")

    monkeypatch.setattr(news, "assert_public_host", fake_assert_public_host)
    monkeypatch.setattr(news, "_feed_client", lambda ctx, public_only: contextlib.nullcontext(ctx.client))


async def run(collector: GoogleNewsCollector | RssCollector, company: Company, handler: FakeWeb, *,
              since: datetime = SINCE, max_items: int = 200) -> tuple[list[RawSignal], CollectContext]:
    collector.request_interval = 0
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"User-Agent": "OpenBerry-test"}) as client:
        ctx = CollectContext(client=client, since=since, settings=get_settings(), max_items=max_items)
        signals = await collector.collect(company, ctx)
    return signals, ctx


def configure(company: Company, *, competitors: list[str] | None = None, **signals: Any) -> Company:
    update: dict[str, Any] = {"signals": company.signals.model_validate({**company.signals.model_dump(), **signals})}
    if competitors is not None:
        update["competitors"] = competitors
    return company.model_copy(update=update)


def by_title(signals: list[RawSignal]) -> dict[str, RawSignal]:
    return {r.signal.title: r for r in signals}


# --------------------------------------------------------------------------------------
# Registration and configuration
# --------------------------------------------------------------------------------------


def test_registered_with_declared_metadata():
    gn = COLLECTORS["google_news"]
    rss = COLLECTORS["rss"]
    assert isinstance(gn, GoogleNewsCollector) and isinstance(rss, RssCollector)
    assert gn.signal_types == ("funding", "company_news", "job_change")
    assert rss.signal_types == ("funding", "job_change", "company_news", "keyword_mention")
    assert gn.requires.startswith("news_queries") and "rss_feeds" in rss.requires
    assert "non-commercial" in gn.requires  # the feed's own terms, shown in the UI where the source is set up


def test_google_news_is_configured(company):
    collector = GoogleNewsCollector()
    assert collector.is_configured(company)
    assert not collector.is_configured(configure(company, news_queries=[]))
    assert not collector.is_configured(configure(company, news_queries="  ,  "))
    assert not collector.enabled_for(configure(company, enabled_types=["hiring"]))
    assert collector.enabled_for(configure(company, enabled_types=["job_change"]))


def test_rss_is_configured(company):
    collector = RssCollector()
    assert collector.is_configured(company)
    assert not collector.is_configured(configure(company, rss_feeds=[]))
    assert not collector.is_configured(configure(
        company, rss_feeds=["ftp://example.com/feed", "file:///etc/passwd", "javascript:alert(1)", "example.com/rss"]))
    # A feed alone is not enough: entries are only kept when they mention something we track.
    bare = configure(company, competitors=[], keywords=[], news_queries=[])
    assert not collector.is_configured(bare)
    assert collector.is_configured(configure(bare, news_queries=["fleet electrification"]))
    assert collector.is_configured(configure(bare, competitors=["Blacklane"]))


# --------------------------------------------------------------------------------------
# Google News
# --------------------------------------------------------------------------------------


async def test_google_news_sends_expected_requests(company):
    company = configure(company, news_queries=[SERIES_A, DUBAI, '"Acme Bank" when:1d'])
    handler = FakeWeb()
    await run(GoogleNewsCollector(), company, handler)
    assert len(handler.requests) == 3
    for request in handler.requests:
        assert request.method == "GET"
        assert f"{request.url.scheme}://{request.url.host}{request.url.path}" == GN
        assert set(request.url.params) == {"q", "hl", "gl", "ceid"}
        assert request.url.params["hl"] == "en-US"
        assert request.url.params["gl"] == "US"
        assert request.url.params["ceid"] == "US:en"
        assert request.headers["accept"].startswith("application/rss+xml")
        assert request.headers["user-agent"] == "OpenBerry-test"
    # 14.5 days between SINCE and NOW -> when:15d; a query with its own when: is left alone.
    assert handler.queries == [f"{SERIES_A} when:15d", f"{DUBAI} when:15d", '"Acme Bank" when:1d']


async def test_google_news_maps_funding_headlines(company):
    signals, ctx = await run(GoogleNewsCollector(), configure(company, news_queries=[SERIES_A]), FakeWeb())
    assert ctx.warnings == []
    found = by_title(signals)
    assert set(found) == {
        "Acme Pay raises $18M Series A to automate B2B invoicing",
        "Ledgerly secures €12 million seed round led by Atomico",
        "Why Series A rounds are getting harder for fintech founders",
        "Former Stripe exec's startup Brightline raises $30M Series B",
        "Blacklane raises $50M to expand chauffeur service across the Gulf",
    }  # Paywise (1 Sep) is older than `since`; the Yahoo Finance copy of Acme Pay is collapsed

    acme = found["Acme Pay raises $18M Series A to automate B2B invoicing"]
    sig = acme.signal
    assert sig.type == "funding" and sig.source == "google_news"
    assert sig.external_id == gn_id("CBMiwwFBVV95cUxQbzdveE9lYkpKaUt6czRZ")  # earliest copy (TechCrunch)
    assert sig.url == "https://news.google.com/rss/articles/CBMiwwFBVV95cUxQbzdveE9lYkpKaUt6czRZ?oc=5"
    assert sig.occurred_at == datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
    assert sig.strength == 70
    assert sig.summary == ("TechCrunch: Acme Pay raises $18M Series A to automate B2B invoicing. "
                           "Found by the Google News search “Series A fintech”.")
    assert sig.raw == {
        "query": SERIES_A, "queries": [SERIES_A], "publisher": "TechCrunch",
        "publisher_url": "https://techcrunch.com", "guid": "CBMiwwFBVV95cUxQbzdveE9lYkpKaUt6czRZ",
        "account": "Acme Pay", "amount": "$18M", "round": "Series A", "also_in": ["Yahoo Finance"],
    }
    assert acme.lead is None and acme.account == "Acme Pay" and acme.account_domain == ""

    ledgerly = found["Ledgerly secures €12 million seed round led by Atomico"]
    assert (ledgerly.account, ledgerly.signal.raw["amount"], ledgerly.signal.raw["round"]) == \
        ("Ledgerly", "€12 million", "Seed")
    assert found["Former Stripe exec's startup Brightline raises $30M Series B"].account == "Brightline"

    topic = found["Why Series A rounds are getting harder for fintech founders"]
    assert (topic.signal.type, topic.account, topic.lead, topic.signal.strength) == ("company_news", "", None, 40)

    # Competitor news stays visible in the feed but never turns the competitor into a lead.
    rival = found["Blacklane raises $50M to expand chauffeur service across the Gulf"]
    assert rival.signal.type == "funding" and rival.account == "" and rival.lead is None
    assert rival.signal.raw["competitor"] == "Blacklane" and rival.signal.strength == 40


async def test_google_news_maps_appointments_and_company_news(company):
    signals, ctx = await run(GoogleNewsCollector(), company, FakeWeb())
    assert ctx.warnings == []
    found = by_title(signals)
    assert len(signals) == 6

    hire = found["Acme Bank appoints Sara Al Mansoori as Head of Corporate Travel"]
    assert hire.signal.type == "job_change"
    assert hire.lead == LeadIn(full_name="Sara Al Mansoori", title="Head of Corporate Travel",
                               lead_company="Acme Bank", source="google_news")
    assert hire.signal.strength == 75  # senior role 65 + keyword "corporate travel" in the headline
    assert hire.signal.raw["person"] == "Sara Al Mansoori" and hire.signal.raw["publisher"] == "Khaleej Times"

    cfo = found["Fintech firm Ledgerly names new CFO ahead of Dubai office opening"]
    assert (cfo.signal.type, cfo.account, cfo.lead, cfo.signal.raw["role"], cfo.signal.strength) == \
        ("job_change", "Ledgerly", None, "CFO", 65)

    office = found["Northwind Capital opens Dubai office to serve Gulf clients"]
    assert (office.signal.type, office.account, office.signal.strength) == ("company_news", "Northwind Capital", 60)
    title_case = found["Globex Logistics Expands To Dubai With New Regional HQ"]
    assert (title_case.account, title_case.signal.strength) == ("Globex Logistics", 60)

    market = found["Dubai office market: rents climb as new openings slow"]
    assert (market.signal.type, market.account, market.signal.strength) == ("company_news", "", 40)
    rival = found["Blacklane opens new Dubai office"]
    assert rival.account == "" and rival.signal.raw["competitor"] == "Blacklane"


async def test_google_news_respects_enabled_types(company):
    company = configure(company, news_queries=[SERIES_A, DUBAI], enabled_types=["funding", "keyword_mention"])
    signals, _ = await run(GoogleNewsCollector(), company, FakeWeb())
    assert signals and {r.signal.type for r in signals} == {"funding"}


async def test_google_news_only_returns_items_newer_than_since(company):
    since = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
    handler = FakeWeb()
    signals, _ = await run(GoogleNewsCollector(), configure(company, news_queries=[SERIES_A]), handler, since=since)
    assert {r.signal.title for r in signals} == {
        "Acme Pay raises $18M Series A to automate B2B invoicing",
        "Ledgerly secures €12 million seed round led by Atomico",
    }
    assert all(r.signal.occurred_at > since for r in signals)
    assert handler.queries == [f"{SERIES_A} when:3d"]


async def test_google_news_dedupes_across_queries_with_stable_ids(company):
    company = configure(company, news_queries=[SERIES_A, "fintech Series A"])
    handler = FakeWeb(gn={"fintech Series A": "google_news_series_a.xml"})
    first, _ = await run(GoogleNewsCollector(), company, handler)
    second, _ = await run(GoogleNewsCollector(), company, FakeWeb(gn={"fintech Series A": "google_news_series_a.xml"}))
    assert len(first) == 5
    assert [r.signal.external_id for r in first] == [r.signal.external_id for r in second]
    assert len({r.signal.external_id for r in first}) == 5
    acme = by_title(first)["Acme Pay raises $18M Series A to automate B2B invoicing"]
    assert acme.signal.raw["queries"] == [SERIES_A, "fintech Series A"]
    assert acme.signal.raw["also_in"] == ["Yahoo Finance"]


async def test_google_news_caps_queries_per_scan(company):
    queries = [f"topic {i}" for i in range(12)]
    handler = FakeWeb()
    _, ctx = await run(GoogleNewsCollector(), configure(company, news_queries=queries), handler)
    assert len(handler.requests) == news.MAX_QUERIES_PER_SCAN == 10
    assert len(set(handler.queries)) == 10
    assert any("at most 10" in w for w in ctx.warnings)


async def test_google_news_respects_max_items(company):
    handler = FakeWeb()
    signals, _ = await run(GoogleNewsCollector(), configure(company, news_queries=[SERIES_A, DUBAI]), handler,
                           max_items=2)
    assert len(signals) == 2
    assert len(handler.requests) == 1  # full after the first search: the second is never sent


@pytest.mark.parametrize("response, expected", [
    (html(403), "Google News: search “Series A fintech” was refused (HTTP 403); skipped the remaining searches"),
    (httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "120"}),
     "was rate limited (HTTP 429, retry after 120s); skipped the remaining searches"),
    (html(503), "was refused (HTTP 503)"),
])
async def test_google_news_stops_when_blocked(company, response, expected):
    handler = FakeWeb(gn={SERIES_A: response})
    signals, ctx = await run(GoogleNewsCollector(), configure(company, news_queries=[SERIES_A, DUBAI]), handler)
    assert signals == []
    assert len(handler.requests) == 1
    assert len(ctx.warnings) == 1 and expected in ctx.warnings[0]


def _raise(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


@pytest.mark.parametrize("failure, expected", [
    (httpx.Response(500, text="Internal Server Error"), "Google News: search “Series A fintech” returned HTTP 500"),
    (_raise(httpx.ReadTimeout("timed out")), "Google News: search “Series A fintech” timed out"),
    (_raise(httpx.ConnectError("boom")), "Google News: search “Series A fintech” failed (ConnectError)"),
    (html(200), "Google News: search “Series A fintech” did not return an RSS/Atom feed"),
    (xml(b"<?xml version='1.0'?><rss version='2.0'><chan"),
     "Google News: search “Series A fintech” returned malformed XML"),
])
async def test_google_news_failed_search_warns_and_continues(company, failure, expected):
    handler = FakeWeb(gn={SERIES_A: failure})
    signals, ctx = await run(GoogleNewsCollector(), configure(company, news_queries=[SERIES_A, DUBAI]), handler)
    assert ctx.warnings == [expected]
    assert len(handler.requests) == 2
    assert len(signals) == 6  # the Dubai search still produced its signals


async def test_google_news_stops_after_repeated_failures(company):
    queries = [f"topic {i}" for i in range(5)]
    handler = FakeWeb(gn={q: _raise(httpx.ReadTimeout("slow")) for q in queries})
    signals, ctx = await run(GoogleNewsCollector(), configure(company, news_queries=queries), handler)
    assert signals == [] and len(handler.requests) == 3
    assert ctx.warnings[-1] == "Google News: 3 failed requests in a row; stopped this scan"


async def test_google_news_bad_item_is_skipped(company, monkeypatch):
    real = news.analyze_headline

    def flaky(title: str):
        if title.startswith("Ledgerly"):
            raise ValueError("boom")
        return real(title)

    monkeypatch.setattr(news, "analyze_headline", flaky)
    signals, ctx = await run(GoogleNewsCollector(), configure(company, news_queries=[SERIES_A]), FakeWeb())
    assert len(signals) == 4
    assert ctx.warnings == ["Google News: skipped 1 malformed result for “Series A fintech” (ValueError)"]


async def test_requests_and_host_checks_end_by_the_hard_deadline(company, monkeypatch):
    """A stalled resolver or slow host must not push the collector past run_scan's 120 s cancel."""
    timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return xml("rss_techcrunch.xml")

    async def stalled_resolver(url: str) -> None:
        await asyncio.sleep(3600)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ctx = CollectContext(client=client, since=SINCE, settings=get_settings())
        fetcher = news._FeedFetcher(ctx, "RSS", max_requests=5, interval=0, budget=60, max_failures=5,
                                    block_statuses=frozenset({403, 429}), stop_on_block=False, public_only=False)
        assert fetcher.timeout == news.REQUEST_TIMEOUT
        fetcher.hard_deadline = time.monotonic() + 6       # 6 s left: lookup, connect and read get 2 s each
        assert await fetcher.fetch(TC_FEED, "feed techcrunch.com/feed/") is not None
        assert 0 < timeouts[0]["connect"] <= 2 and 0 < timeouts[0]["read"] <= 2

        monkeypatch.setattr(news, "assert_public_host", stalled_resolver)
        fetcher.public_only = True
        fetcher.hard_deadline = time.monotonic() + 0.6
        started = time.monotonic()
        assert await fetcher.fetch(BLOG_FEED, "feed blog.example.org/feed.atom") is None
        assert time.monotonic() - started < 2

        fetcher.hard_deadline = time.monotonic() + 0.1
        assert await fetcher.fetch(BLOG_FEED, "feed blog.example.org/feed.atom") is None
    assert len(timeouts) == 1
    assert ctx.warnings == ["RSS: feed blog.example.org/feed.atom host lookup timed out",
                            "RSS: feed blog.example.org/feed.atom was skipped: time budget for this scan used up"]


async def test_google_news_time_budget(company):
    collector = GoogleNewsCollector()
    collector.time_budget = 0
    handler = FakeWeb()
    signals, ctx = await run(collector, company, handler)
    assert signals == [] and handler.requests == []
    assert ctx.warnings == ["Google News: time budget for this scan used up; skipped the remaining requests"]


# --------------------------------------------------------------------------------------
# RSS / Atom
# --------------------------------------------------------------------------------------


def rss_company(company: Company, **signals: Any) -> Company:
    return configure(company, rss_feeds=[TC_FEED, BLOG_FEED], **signals)


async def test_rss_sends_expected_requests(company):
    handler = FakeWeb()
    await run(RssCollector(), rss_company(company), handler)
    assert handler.urls == [TC_FEED, BLOG_FEED]
    for request in handler.requests:
        assert request.method == "GET" and not request.url.params
        assert request.headers["accept"].startswith("application/rss+xml")
        assert request.headers["user-agent"] == "OpenBerry-test"


async def test_rss_maps_matching_entries(company):
    signals, ctx = await run(RssCollector(), rss_company(company), FakeWeb())
    assert ctx.warnings == []
    found = by_title(signals)
    assert set(found) == {
        "Chauffeur marketplace Ridely raises $25M Series B to expand across the Gulf",
        "Blacklane partners with Emirates for premium airport transfers",
        "How corporate travel managers are rethinking ground transport in 2026",
        "Northwind hires former Uber exec as VP of corporate travel",
        "Acme Corp launches expense tool for finance teams",
        "Vela Mobility wants to be the Uber of chauffeurs, and investors are buying it",
        "Careem Business adds monthly invoicing for SMEs",
        "Initech closes $40 million Series C led by Sequoia",
    }  # Apple (no match), Limozy (too old) and the undated note are dropped

    ridely = found["Chauffeur marketplace Ridely raises $25M Series B to expand across the Gulf"]
    sig = ridely.signal
    assert (sig.type, sig.source, sig.strength) == ("funding", "rss", 80)  # amount 70 + keyword in headline
    assert sig.external_id == rss_id("https://techcrunch.com/?p=3051001")
    assert sig.url == "https://techcrunch.com/2026/10/08/chauffeur-marketplace-ridely-raises-25m-series-b/"
    assert sig.occurred_at == datetime(2026, 10, 8, 7, 30, tzinfo=timezone.utc)
    assert sig.summary.startswith("Ridely, which connects corporate clients with vetted chauffeur companies")
    assert "<b>" not in sig.summary
    assert sig.raw == {
        "feed": TC_FEED, "feed_title": "TechCrunch", "matched": ["chauffeur"], "matched_in": "title",
        "categories": ["Fundraising", "Startups", "Transportation", "ridely"], "author": "Ingrid Lunden",
        "account": "Ridely", "amount": "$25M", "round": "Series B",
    }
    assert ridely.account == "Ridely" and ridely.lead is None

    topic = found["How corporate travel managers are rethinking ground transport in 2026"]
    assert (topic.signal.type, topic.account, topic.lead, topic.signal.strength) == ("keyword_mention", "", None, 40)

    hire = found["Northwind hires former Uber exec as VP of corporate travel"]
    assert (hire.signal.type, hire.account, hire.lead, hire.signal.strength) == ("job_change", "Northwind", None, 75)
    assert hire.signal.raw["role"] == "VP of corporate travel"

    weak = found["Acme Corp launches expense tool for finance teams"]
    assert (weak.signal.type, weak.account, weak.signal.strength) == ("company_news", "Acme Corp", 35)
    assert weak.signal.raw["matched_in"] == "summary"

    category = found["Vela Mobility wants to be the Uber of chauffeurs, and investors are buying it"]
    assert (category.signal.type, category.account) == ("funding", "")  # from the "Fundraising" category

    for title, rival in [("Blacklane partners with Emirates for premium airport transfers", "Blacklane"),
                         ("Careem Business adds monthly invoicing for SMEs", "Careem Business")]:
        r = found[title]
        assert (r.signal.type, r.account, r.lead, r.signal.raw["competitor"]) == ("company_news", "", None, rival)

    careem = found["Careem Business adds monthly invoicing for SMEs"].signal
    assert careem.url == "https://blog.example.org/2026/10/careem-business-invoicing"  # relative link resolved
    assert careem.occurred_at == datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)   # Atom <updated>
    assert careem.external_id == rss_id("tag:blog.example.org,2026:post-41")
    assert careem.raw["author"] == "Layla Haddad" and careem.raw["feed_title"] == "Gulf Mobility Weekly"

    initech = found["Initech closes $40 million Series C led by Sequoia"]
    assert initech.signal.type == "funding" and initech.account == "Initech"
    assert initech.signal.raw["matched"] == [DUBAI] and initech.signal.raw["matched_in"] == "summary"
    assert initech.signal.strength == 60  # 70 - 10: the news query only matched in the article body
    assert initech.signal.occurred_at == datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)  # <published> wins


async def test_rss_keeps_only_entries_matching_terms(company):
    only_competitors = rss_company(company, keywords=[], news_queries=[])
    signals, _ = await run(RssCollector(), only_competitors, FakeWeb())
    assert {r.signal.title for r in signals} == {
        "Blacklane partners with Emirates for premium airport transfers",
        "Careem Business adds monthly invoicing for SMEs",
    }


async def test_rss_without_terms_warns_and_fetches_nothing(company):
    handler = FakeWeb()
    signals, ctx = await run(RssCollector(), rss_company(company, keywords=[], news_queries=[], competitors=[]),
                             handler)
    assert signals == [] and handler.requests == []
    assert ctx.warnings and "add keywords, competitors or news queries" in ctx.warnings[0]


async def test_rss_rejects_non_http_feed_urls(company):
    company = configure(company, rss_feeds=["ftp://example.com/feed.xml", "file:///etc/passwd",
                                            "javascript:alert(1)", "example.com/rss", TC_FEED])
    handler = FakeWeb()
    signals, ctx = await run(RssCollector(), company, handler)
    assert handler.urls == [TC_FEED] and signals
    assert len(ctx.warnings) == 4
    assert all("not a valid http:// or https:// feed URL" in w for w in ctx.warnings)


async def test_rss_invalid_host_names_never_sink_the_scan(company, monkeypatch):
    """Host names the resolver cannot encode used to raise UnicodeError out of collect(), losing every feed."""
    from openberry import website

    monkeypatch.setattr(news, "assert_public_host", website.assert_public_host)  # the real check
    long_label = "https://" + "a" * 64 + ".example.com/rss?token=SECRET"   # 64-char label: invalid syntax
    unicode_label = "https://" + "ü" * 60 + ".example.com/feed"           # valid syntax, IDNA "label too long"
    redirect = "https://redirect.example.com/feed"
    handler = FakeWeb(feeds={redirect: httpx.Response(301, headers={"Location": "https://" + "b" * 70 + ".io/rss"})})
    company = configure(company, rss_feeds=[long_label, "https://exa..mple.com/rss", unicode_label, redirect, TC_FEED])
    assert RssCollector().is_configured(company)

    async def tc_is_public(url: str) -> None:  # no DNS in the sandbox: resolve only the hosts that need it
        if "techcrunch.com" not in url and "redirect.example.com" not in url:
            await website.assert_public_host(url)

    monkeypatch.setattr(news, "assert_public_host", tc_is_public)
    signals, ctx = await run(RssCollector(), company, handler)
    assert handler.urls == [redirect, TC_FEED]
    assert len(signals) == 6
    assert ctx.warnings == [
        f"RSS: skipped “{news._redact(long_label)}”: not a valid http:// or https:// feed URL",
        "RSS: skipped “https://exa..mple.com/rss”: not a valid http:// or https:// feed URL",
        f"RSS: feed {news.feed_label(unicode_label)} skipped: cannot check host (UnicodeEncodeError)",
        "RSS: feed redirect.example.com/feed skipped: invalid host name",
    ]
    assert not any("SECRET" in w for w in ctx.warnings)


async def test_naive_since_is_treated_as_utc(company):
    naive = SINCE.replace(tzinfo=None)
    gn_signals, gn_ctx = await run(GoogleNewsCollector(), company, FakeWeb(), since=naive)
    rss_signals, rss_ctx = await run(RssCollector(), company, FakeWeb(), since=naive)
    assert gn_ctx.warnings == [] and rss_ctx.warnings == []
    assert (len(gn_signals), len(rss_signals)) == (6, 6)
    assert all(r.signal.occurred_at > SINCE for r in gn_signals + rss_signals)


async def test_google_news_when_matches_the_lookback(company):
    # run_scan sets since = now - lookback_days a moment before the collector reads the clock.
    handler = FakeWeb()
    await run(GoogleNewsCollector(), company, handler, since=NOW - timedelta(days=14, seconds=3))
    assert handler.queries == [f"{DUBAI} when:14d"]


async def test_google_news_price_raises_and_people_are_mapped_correctly(company):
    company = configure(company, news_queries=["Dubai chauffeur"])
    handler = FakeWeb(gn={"Dubai chauffeur": "google_news_people_and_prices.xml"})
    signals, ctx = await run(GoogleNewsCollector(), company, handler)
    assert ctx.warnings == []
    found = by_title(signals)

    fare = found["Uber raises minimum fare to AED 12 in Dubai"]
    assert (fare.signal.type, fare.account, fare.signal.strength) == ("company_news", "Uber", 45)
    assert "amount" not in fare.signal.raw

    jane = found["Acme Bank names Google's Jane Doe as CTO"]
    assert jane.lead == LeadIn(full_name="Jane Doe", title="CTO", lead_company="Acme Bank", source="google_news")
    omar = found["Globex promotes CFO Omar Haddad to CEO"]
    assert omar.lead == LeadIn(full_name="Omar Haddad", title="CEO", lead_company="Globex", source="google_news")
    assert omar.signal.raw["role"] == "CEO"

    zepto = found["India's Zepto raises $450 million to expand to Dubai"]
    assert (zepto.signal.type, zepto.account, zepto.signal.raw["amount"]) == ("funding", "Zepto", "$450 million")


async def test_rss_refuses_private_hosts_and_unsafe_redirects(company):
    redirect_to_metadata = httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    redirect_to_file = httpx.Response(301, headers={"Location": "file:///etc/passwd"})
    company = configure(company, rss_feeds=["http://10.0.0.5/feed", "http://intranet.local/rss",
                                            "https://redirect.example.com/a", "https://redirect.example.com/b"])
    handler = FakeWeb(feeds={"https://redirect.example.com/a": redirect_to_metadata,
                             "https://redirect.example.com/b": redirect_to_file})
    signals, ctx = await run(RssCollector(), company, handler)
    assert signals == []
    assert handler.urls == ["https://redirect.example.com/a", "https://redirect.example.com/b"]  # never 10.x/169.254
    assert ctx.warnings == [
        "RSS: feed 10.0.0.5/feed skipped: 10.0.0.5 resolves to a non-public address (only public hosts are fetched)",
        "RSS: feed intranet.local/rss skipped: intranet.local resolves to a non-public address "
        "(only public hosts are fetched)",
        "RSS: feed redirect.example.com/a skipped: 169.254.169.254 resolves to a non-public address "
        "(only public hosts are fetched)",
        "RSS: feed redirect.example.com/b redirected to a non-http(s) URL",
    ]


async def test_rss_private_hosts_allowed_when_settings_say_so(company):
    settings = get_settings()
    settings.allow_private_feeds = True  # forward-compatible flag (see the module docstring)
    try:
        handler = FakeWeb(feeds={"http://localhost/rsshub/feed": xml("rss_techcrunch.xml")})
        signals, ctx = await run(RssCollector(), configure(company, rss_feeds=["http://localhost/rsshub/feed"]),
                                 handler)
    finally:
        del settings.allow_private_feeds
    assert handler.urls == ["http://localhost/rsshub/feed"] and signals and ctx.warnings == []


async def test_public_only_feeds_get_a_client_that_checks_addresses_when_connecting():
    """A host check before each request can be dodged by DNS rebinding (see tests/test_netguard.py)."""
    async with httpx.AsyncClient(headers={"User-Agent": "OpenBerry-test"}) as shared:
        ctx = CollectContext(client=shared, since=SINCE, settings=get_settings())
        async with REAL_FEED_CLIENT(ctx, False) as client:  # Google News, or allow_private_feeds
            assert client is shared
        async with REAL_FEED_CLIENT(ctx, True) as client:
            assert client is not shared and client.headers["User-Agent"] == "OpenBerry-test"
            assert isinstance(client._transport, netguard.PublicOnlyTransport) and not client._mounts
        assert not shared.is_closed


async def test_rss_follows_safe_redirects(company):
    old = "http://techcrunch.com/feed"
    loop = "https://loop.example.com/feed"
    handler = FakeWeb(feeds={
        old: httpx.Response(301, headers={"Location": TC_FEED}),
        loop: httpx.Response(302, headers={"Location": "/feed"}),
    })
    signals, ctx = await run(RssCollector(), configure(company, rss_feeds=[old, loop]), handler)
    assert handler.urls[:2] == [old, TC_FEED]
    assert len(signals) == 6
    assert ctx.warnings == ["RSS: feed loop.example.com/feed redirected more than 4 times"]
    assert handler.urls.count(loop) == 5


async def test_rss_same_article_in_two_feeds_is_one_signal(company):
    category_feed = "https://techcrunch.com/category/startups/feed/"
    handler = FakeWeb(feeds={category_feed: xml("rss_techcrunch.xml")})
    signals, _ = await run(RssCollector(), configure(company, rss_feeds=[TC_FEED, category_feed]), handler)
    assert len(handler.requests) == 2
    assert len(signals) == 6 and len({r.signal.external_id for r in signals}) == 6
    assert all(r.signal.raw["feed"] == TC_FEED for r in signals)


async def test_rss_feed_cap_per_scan(company):
    feeds = [f"https://feeds{i}.example.com/rss" for i in range(20)]
    handler = FakeWeb(feeds={f: xml(EMPTY_GN) for f in feeds})
    _, ctx = await run(RssCollector(), configure(company, rss_feeds=feeds), handler)
    assert len(handler.requests) == news.MAX_FEEDS_PER_SCAN == 15
    assert ctx.warnings == ["RSS: 20 feeds configured but at most 15 are read per scan; "
                            "the rest rotate in on later scans"]


async def test_rss_respects_max_items(company):
    handler = FakeWeb()
    signals, _ = await run(RssCollector(), rss_company(company), handler, max_items=3)
    assert len(signals) == 3 and handler.urls == [TC_FEED]


async def test_rss_rate_limited_host_is_skipped_for_the_scan(company):
    second = "https://techcrunch.com/category/startups/feed/"
    handler = FakeWeb(feeds={TC_FEED: httpx.Response(429, headers={"Retry-After": "60"})})
    signals, ctx = await run(RssCollector(), configure(company, rss_feeds=[TC_FEED, second, BLOG_FEED]), handler)
    assert handler.urls == [TC_FEED, BLOG_FEED]  # the second techcrunch.com feed is not requested
    assert ctx.warnings == ["RSS: feed techcrunch.com/feed/ was rate limited (HTTP 429, retry after 60s); "
                            "skipped techcrunch.com for the rest of this scan"]
    assert {r.signal.raw["feed"] for r in signals} == {BLOG_FEED}


@pytest.mark.parametrize("failure, expected", [
    (html(403), "RSS: feed techcrunch.com/feed/ was refused (HTTP 403); skipped techcrunch.com for the rest of this scan"),
    (httpx.Response(500, text="oops"), "RSS: feed techcrunch.com/feed/ returned HTTP 500"),
    (_raise(httpx.ReadTimeout("slow")), "RSS: feed techcrunch.com/feed/ timed out"),
    (html(200), "RSS: feed techcrunch.com/feed/ did not return an RSS/Atom feed"),
    (httpx.Response(200, content=b'{"items": []}', headers={"content-type": "application/json"}),
     "RSS: feed techcrunch.com/feed/ did not return an RSS/Atom feed"),
    (xml(b"x" * 100, **{"content-length": str(news.MAX_FEED_BYTES + 1)}), "RSS: feed techcrunch.com/feed/ is larger than 5 MB"),
    (xml(b"<rss>" + b" " * (news.MAX_FEED_BYTES + 10)), "RSS: feed techcrunch.com/feed/ is larger than 5 MB"),
])
async def test_rss_failed_feed_warns_and_continues(company, failure, expected):
    handler = FakeWeb(feeds={TC_FEED: failure})
    signals, ctx = await run(RssCollector(), rss_company(company), handler)
    assert ctx.warnings == [expected]
    assert handler.urls == [TC_FEED, BLOG_FEED]
    assert {r.signal.raw["feed"] for r in signals} == {BLOG_FEED}


async def test_rss_malformed_xml_keeps_readable_items(company):
    broken = "https://broken.example.net/rss"
    handler = FakeWeb(feeds={broken: xml("malformed.xml")})
    signals, ctx = await run(RssCollector(), configure(company, rss_feeds=[broken]), handler)
    assert ctx.warnings == ["RSS: feed broken.example.net/rss returned malformed XML; used the 2 items that could be read"]
    assert [r.signal.title for r in signals] == ["Chauffeur demand soars in Dubai"]
    assert signals[0].signal.type == "keyword_mention"


async def test_rss_bad_entries_are_skipped_with_one_warning(company, monkeypatch):
    real = news.analyze_headline

    def flaky(title: str):
        if title.startswith(("Blacklane", "Northwind")):
            raise ValueError("boom")
        return real(title)

    monkeypatch.setattr(news, "analyze_headline", flaky)
    signals, ctx = await run(RssCollector(), configure(company, rss_feeds=[TC_FEED]), FakeWeb())
    assert len(signals) == 4
    assert ctx.warnings == ["RSS: skipped 2 malformed entries in techcrunch.com/feed/ (ValueError)"]


async def test_rss_stops_after_repeated_failures(company):
    feeds = [f"https://down{i}.example.com/rss" for i in range(8)]
    handler = FakeWeb(feeds={f: _raise(httpx.ConnectError("no route")) for f in feeds})
    signals, ctx = await run(RssCollector(), configure(company, rss_feeds=feeds), handler)
    assert signals == [] and len(handler.requests) == 5
    assert ctx.warnings[-1] == "RSS: 5 failed requests in a row; stopped this scan"


async def test_rss_feed_urls_in_warnings_hide_secrets(company):
    secret = "https://user:hunter2@private-feeds.example.com/rss?token=SECRET"
    handler = FakeWeb(feeds={secret: httpx.Response(500)})
    _, ctx = await run(RssCollector(), configure(company, rss_feeds=[secret]), handler)
    assert ctx.warnings == ["RSS: feed private-feeds.example.com/rss returned HTTP 500"]


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("title, kind, account, person, role", [
    ("Acme Pay raises $18M Series A to automate B2B invoicing", "funding", "Acme Pay", "", ""),
    ("Dubai-based fintech Zeta Pay has raised $7 million", "funding", "Zeta Pay", "", ""),
    ("Exclusive: Vela Mobility lands $12M to take on Uber in the Gulf", "funding", "Vela Mobility", "", ""),
    ("Fintech Firm Ledgerly Raises $5M", "funding", "Ledgerly", "", ""),
    ("Initech closes $40 million Series C led by Sequoia", "funding", "Initech", "", ""),
    ("Pied Piper's Series A extension led by Raviga", "funding", "", "", ""),
    ("Acme secures $10M contract with Dubai RTA", "", "Acme", "", ""),
    ("Acme closes acquisition of Globex", "", "Acme", "", ""),
    ("Why Series A rounds are getting harder for fintech founders", "", "", "", ""),
    ("Investors Pour Money As Acme Raises $5M", "funding", "", "", ""),
    ("Acme Names Jane Doe As Chief Technology Officer", "job_change", "Acme", "Jane Doe", "Chief Technology Officer"),
    ("Acme Appoints Former Google Exec As CTO", "job_change", "Acme", "", "CTO"),
    ("Jane Doe joins Acme Pay as Chief Revenue Officer", "job_change", "Acme Pay", "Jane Doe", "Chief Revenue Officer"),
    ("Jane Doe named CEO of Initech Systems", "job_change", "Initech Systems", "Jane Doe", "CEO"),
    ("Hooli promotes Gavin Belson to President", "job_change", "Hooli", "Gavin Belson", "President"),
    ("Bank of Dubai names Omar bin Rashid chief executive officer", "job_change", "Bank of Dubai", "Omar bin Rashid",
     "chief executive officer"),
    ("Acme hires 200 engineers as headcount grows", "", "Acme", "", ""),
    ("Raviga Capital to open Riyadh office in 2027", "", "Raviga Capital", "", ""),
    ("eToro unveils new trading app", "", "eToro", "", ""),
    ("How corporate travel managers are rethinking ground transport", "", "", "", ""),
    ("Report: Acme In Talks To Raise $1B", "funding", "", "", ""),
    # Raising prices, fares, forecasts, bids or stakes is not a funding round.
    ("Uber raises minimum fare to AED 12 in Dubai", "", "Uber", "", ""),
    ("Netflix raises subscription prices to $17.99", "", "Netflix", "", ""),
    ("Careem has raised fares in Dubai", "", "Careem", "", ""),
    ("Acme raises 2026 revenue forecast to $5 billion", "", "Acme", "", ""),
    ("Microsoft raises bid for Activision to $70 billion", "", "Microsoft", "", ""),
    ("Acme raised its stake in Globex to $1B", "", "Acme", "", ""),
    ("Tesla receives $500 million order from Hertz", "", "Tesla", "", ""),
    ("Acme raises $5M in debt financing", "funding", "Acme", "", ""),
    ("Acme receives $2M grant", "funding", "Acme", "", ""),
    # Places are not accounts; a place in the possessive or a leading descriptor is not part of the name.
    ("Abu Dhabi launches new tourism visa", "", "", "", ""),
    ("Saudi Arabia secures $5bn loan from banks", "funding", "", "", ""),
    ("U.S. names new ambassador to UAE", "", "", "", ""),
    ("India's Zepto raises $450 million", "funding", "Zepto", "", ""),
    ("Abu Dhabi's ADQ invests $1B in fund", "", "ADQ", "", ""),
    ("Startup Acme Raises $5M Seed Round", "funding", "Acme", "", ""),
    ("Google opens new Dubai office", "", "Google", "", ""),
    ("Emirates launches new Dubai chauffeur service", "", "Emirates", "", ""),
    # The employer in the possessive is not part of the person's name; the new role follows the person.
    ("Acme names Google's Jane Doe as CTO", "job_change", "Acme", "Jane Doe", "CTO"),
    ("Acme promotes CFO Jane Doe to CEO", "job_change", "Acme", "Jane Doe", "CEO"),
    ("Acme Announces Appointment Of Jane Doe As Chief Financial Officer", "job_change", "Acme", "Jane Doe",
     "Chief Financial Officer"),
])
def test_analyze_headline(title, kind, account, person, role):
    info = analyze_headline(title)
    assert (info.kind, info.account, info.person, info.role) == (kind, account, person, role)


def test_headline_details_and_strength_rules():
    funding = analyze_headline("Ledgerly secures €12 million seed round led by Atomico")
    assert (funding.amount, funding.round) == ("€12 million", "Seed")
    assert news_strength("funding", funding, has_target=True) == 70
    assert news_strength("funding", analyze_headline("Acme has raised money"), has_target=True) == 55
    assert news_strength("job_change", analyze_headline("Acme names Jane Doe as CTO"), has_target=True) == 65
    assert news_strength("job_change", analyze_headline("Acme names Jane Doe director of sales"), has_target=True) == 55
    expansion = analyze_headline("Northwind Capital opens Dubai office")
    assert expansion.expansion and news_strength("company_news", expansion, has_target=True) == 60
    assert not analyze_headline("Acme launches open-source SDK").expansion
    assert analyze_headline("Acme enters Saudi market").expansion
    distress = analyze_headline("Acme enters administration")  # used to score as expansion news (60)
    assert not distress.expansion and news_strength("company_news", distress, has_target=True) == 45
    assert not analyze_headline("Acme expands layoffs to Dubai office").expansion
    assert news_strength("company_news", analyze_headline("Acme ships SDK"), has_target=True) == 45
    assert news_strength("keyword_mention", analyze_headline("x"), has_target=False) == 40
    assert news_strength("funding", funding, has_target=True, topic_in_title=True) == 80
    assert news_strength("company_news", analyze_headline("Acme ships SDK"), has_target=True, summary_only=True) == 35
    assert news_strength("funding", funding, has_target=False, topic_in_title=True) == 40
    assert news_strength("keyword_mention", analyze_headline("x"), has_target=False, summary_only=True) == 30


def test_split_publisher():
    assert split_publisher("Acme raises $5M - TechCrunch", "TechCrunch") == ("Acme raises $5M", "TechCrunch")
    assert split_publisher("Acme - the startup - raises $5M - Gulf News", "") == \
        ("Acme - the startup - raises $5M", "Gulf News")
    assert split_publisher("No publisher suffix here", "Reuters") == ("No publisher suffix here", "Reuters")


@pytest.mark.parametrize("query, text, expected", [
    ("Dubai office opening", "Initech plans its Dubai office opening", True),
    ("Dubai office opening", "Opening of an office in Dubai", True),          # every word, any order
    ("Dubai office opening", "Dubai office market cools", False),
    ('"Series A" fintech', "Fintech Acme raises a Series A", True),
    ('"Series A" fintech', "Fintech Acme raises a Series B", False),
    ("(fintech OR insurtech) raises", "Insurtech Zeta raises $5M", True),
    ("chauffeur -jobs", "Chauffeur jobs in Dubai", False),
    ("chauffeur -jobs", "Chauffeur demand soars", True),
    ("chauffeur when:7d site:techcrunch.com", "Chauffeur demand soars", True),  # Google-only operators ignored
    ("intitle:limousine", "Limousine fleet expands", True),
    ("when:7d", "anything", False),
])
def test_query_matcher(query, text, expected):
    assert QueryMatcher.parse(query).matches(text) is expected


def test_parse_feed_never_opens_urls_or_files():
    # feedparser.parse("<a URL or path>") would fetch/open it; parse_feed always hands it bytes.
    for body in (b"/etc/passwd", b"http://169.254.169.254/latest/meta-data/", b"file:///etc/hostname"):
        parsed, problem = parse_feed(body, "https://example.com/feed")
        assert parsed.entries == [] and problem == "did not return an RSS/Atom feed"


def test_entry_ids_are_scoped_when_guids_are_not_urls():
    bare = {"id": "12345", "link": "https://a.example.com/p/12345"}
    assert news._entry_key(bare, "https://a.example.com/rss") == "a.example.com|12345"
    assert news._entry_key(bare, "https://b.example.com/rss") == "b.example.com|12345"
    assert news._entry_key({"id": "https://techcrunch.com/?p=1"}, TC_FEED) == "https://techcrunch.com/?p=1"
    assert news._entry_key({"link": "https://x.example.com/a"}, TC_FEED) == "https://x.example.com/a"


# --------------------------------------------------------------------------------------
# End to end: collectors -> services.ingest -> scored leads
# --------------------------------------------------------------------------------------


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def frozen_scan_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture dates are absolute (Oct 2026): pin the scan window and intent decay to NOW."""
    from openberry import scoring

    monkeypatch.setattr(repo, "utcnow", lambda: NOW)
    monkeypatch.setattr(scoring, "datetime", _FrozenDatetime)


async def test_ingest_creates_scored_leads(company, frozen_scan_clock):
    gn_signals, gn_ctx = await run(GoogleNewsCollector(), company, FakeWeb())
    rss_signals, rss_ctx = await run(RssCollector(), company, FakeWeb())  # conftest feed -> TechCrunch fixture
    assert gn_ctx.warnings == [] and rss_ctx.warnings == []
    signals = gn_signals + rss_signals
    assert len(gn_signals) == 6 and len(rss_signals) == 6

    stats = ingest(company.id, signals)
    assert stats.errors == []
    assert stats.signals_new == len(signals)

    people, _ = repo.list_leads(company.id, kind="person")
    sara = next(p for p in people if p.full_name == "Sara Al Mansoori")
    assert (sara.title, sara.lead_company, sara.source) == ("Head of Corporate Travel", "Acme Bank", "google_news")
    assert sara.intent_score > 0 and sara.score > 0 and sara.score_reasons

    accounts, _ = repo.list_leads(company.id, kind="account")
    names = {a.lead_company: a for a in accounts}
    assert {"Northwind Capital", "Ledgerly", "Globex Logistics", "Ridely", "Northwind", "Acme Corp"} <= set(names)
    assert "Blacklane" not in names and "Careem Business" not in names  # competitors never become leads
    assert names["Ridely"].source == "rss" and names["Northwind Capital"].source == "google_news"
    assert names["Ridely"].intent_score > 0 and names["Ridely"].score > 0

    lead_signals, _ = repo.list_signals(company.id, lead_id=names["Ridely"].id)
    assert [s.type for s in lead_signals] == ["funding"]

    gn_stored, gn_total = repo.list_signals(company.id, source="google_news")
    rss_stored, rss_total = repo.list_signals(company.id, source="rss")
    assert (gn_total, rss_total) == (6, 6)
    unattached = {s.title for s in gn_stored + rss_stored if s.lead_id is None}
    assert "Blacklane opens new Dubai office" in unattached
    assert "How corporate travel managers are rethinking ground transport in 2026" in unattached

    again = ingest(company.id, (await run(GoogleNewsCollector(), company, FakeWeb()))[0])
    assert again.signals_new == 0 and again.signals_duplicate == 6


async def test_run_scan_uses_both_news_collectors(company, frozen_scan_clock):
    from openberry.services import run_scan

    handler = FakeWeb()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        stats = await run_scan(company.id, sources=["google_news", "rss"], client=client)
    assert stats["status"] == "ok"
    assert stats["collectors"]["google_news"] == {"found": 6, "warnings": []}
    assert stats["collectors"]["rss"] == {"found": 6, "warnings": []}
    assert stats["signals_new"] == 12 and stats["leads_new"] >= 7
    assert sorted(handler.urls)[0].startswith(CONF_FEED)
