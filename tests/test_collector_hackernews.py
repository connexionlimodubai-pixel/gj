"""Hacker News collector (Algolia HN Search API), fully offline via httpx.MockTransport.

Fixtures in tests/fixtures/hackernews mirror the recorded Algolia payloads from the API research
(search_by_date hits for stories and comments, and the monthly whoishiring threads).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from openberry import repo
from openberry.collectors import COLLECTORS
from openberry.collectors import hackernews as hn
from openberry.collectors.base import CollectContext, RawSignal
from openberry.collectors.hackernews import HackerNewsCollector
from openberry.config import get_settings
from openberry.models import Company
from openberry.services import ingest

FIXTURES = Path(__file__).parent / "fixtures" / "hackernews"
SINCE = datetime(2026, 9, 24, tzinfo=timezone.utc)
SINCE_TS = 1790208000
SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
THREAD = "45400001"        # "Who is hiring? (October 2026)", posted after SINCE
PREV_THREAD = "45100001"   # "Who is hiring? (September 2026)"
# SINCE (Sep 24) is before the October thread was posted, so September's thread is searched too.
HIRING_TAGS = f"comment,(story_{THREAD},story_{PREV_THREAD})"
LATEST_ONLY_TAGS = f"comment,story_{THREAD}"

FINDER = ("story,author_whoishiring", None)
HIRING_EA = (HIRING_TAGS, "Executive Assistant")
HIRING_TRAVEL = (HIRING_TAGS, "Travel")
BLACKLANE = ("(story,comment)", "Blacklane")
CHAUFFEUR = ("(story,comment)", "chauffeur")
CAREEM = ("(story,comment)", "Careem Business")
CORP_TRAVEL = ("(story,comment)", "corporate travel")

ROUTES = {
    FINDER: "whoishiring_stories.json",
    HIRING_EA: "hiring_executive_assistant.json",
    HIRING_TRAVEL: "hiring_travel.json",
    BLACKLANE: "search_blacklane.json",
    CHAUFFEUR: "search_chauffeur.json",
    CAREEM: "search_careem_business.json",
    CORP_TRAVEL: "search_corporate_travel.json",
    (LATEST_ONLY_TAGS, "Executive Assistant"): "hiring_executive_assistant.json",
    (LATEST_ONLY_TAGS, "Travel"): "hiring_travel.json",
}
EMPTY_PAGE = {"hits": [], "hitsPerPage": 50, "nbHits": 0, "nbPages": 0, "page": 0, "query": ""}

Override = httpx.Response | Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeAlgolia:
    """MockTransport handler: serves fixtures by (tags, query), records requests, injects failures."""

    def __init__(self, overrides: dict[tuple[str, str | None], Override] | None = None,
                 default: Override | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides = overrides or {}
        self.default = default

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert f"{request.url.scheme}://{request.url.host}{request.url.path}" == SEARCH
        key = (request.url.params.get("tags"), request.url.params.get("query"))
        override = self.overrides.get(key, self.default)
        if override is not None:
            return override(request) if callable(override) else override
        name = ROUTES.get(key)
        return httpx.Response(200, json=fixture(name) if name else EMPTY_PAGE)

    @property
    def keys(self) -> list[tuple[str | None, str | None]]:
        return [(r.url.params.get("tags"), r.url.params.get("query")) for r in self.requests]


async def run(company: Company, handler: FakeAlgolia, *, since: datetime = SINCE, max_items: int = 200,
              collector: HackerNewsCollector | None = None,
              settings: Any = None) -> tuple[list[RawSignal], CollectContext]:
    collector = collector or HackerNewsCollector()
    collector.request_interval = 0
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"User-Agent": "OpenBerry-test"}) as client:
        ctx = CollectContext(client=client, since=since, settings=settings or get_settings(), max_items=max_items)
        signals = await collector.collect(company, ctx)
    return signals, ctx


def by_id(signals: list[RawSignal]) -> dict[str, RawSignal]:
    return {r.signal.external_id: r for r in signals}


def configure(company: Company, *, competitors: list[str] | None = None, **signals: Any) -> Company:
    update: dict[str, Any] = {"signals": company.signals.model_copy(update=signals)}
    if competitors is not None:
        update["competitors"] = competitors
    return company.model_copy(update=update)


ALL_IDS = {
    "hn:hiring:45401001", "hn:hiring:45401003", "hn:hiring:45401010", "hn:hiring:45101007",
    "hn:45410001", "hn:45410002", "hn:45410003", "hn:45410008",
    "hn:45410020", "hn:45410021", "hn:45410030",
}


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_registered_with_declared_metadata():
    collector = COLLECTORS["hackernews"]
    assert isinstance(collector, HackerNewsCollector)
    assert collector.signal_types == ("competitor_engagement", "keyword_mention", "hiring")
    assert collector.label == "Hacker News"


def test_is_configured(company):
    collector = HackerNewsCollector()
    assert collector.is_configured(company)
    bare = configure(company, competitors=[], keywords=[], hiring_keywords=[])
    assert not collector.is_configured(bare)
    assert not collector.enabled_for(bare)
    assert collector.is_configured(configure(bare, competitors=["Blacklane"]))
    assert collector.is_configured(configure(bare, keywords=["chauffeur"]))
    assert collector.is_configured(configure(bare, hiring_keywords=["Travel Manager"]))
    # Configured but every type it emits is switched off.
    assert not collector.enabled_for(configure(company, enabled_types=["funding", "github_star"]))


# --------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------


async def test_sends_expected_requests(company):
    api = FakeAlgolia()
    _, ctx = await run(company, api)
    assert ctx.warnings == []
    # Hiring first (find the thread, then one search per hiring keyword), then competitors and
    # keywords interleaved.
    assert api.keys == [FINDER, HIRING_EA, HIRING_TRAVEL, BLACKLANE, CHAUFFEUR, CAREEM, CORP_TRAVEL]
    params = [dict(r.url.params) for r in api.requests]
    assert params[0] == {"tags": "story,author_whoishiring", "hitsPerPage": "10"}
    assert params[1] == {"query": "Executive Assistant", "tags": HIRING_TAGS,
                         "numericFilters": f"created_at_i>{SINCE_TS}", "hitsPerPage": "100"}
    assert str(api.requests[1].url) == (
        f"{SEARCH}?query=Executive+Assistant&tags=comment%2C%28story_{THREAD}%2Cstory_{PREV_THREAD}%29"
        f"&numericFilters=created_at_i%3E{SINCE_TS}&hitsPerPage=100")
    assert params[3] == {"query": "Blacklane", "tags": "(story,comment)",
                         "numericFilters": f"created_at_i>{SINCE_TS}", "hitsPerPage": "50"}
    assert str(api.requests[3].url) == (
        f"{SEARCH}?query=Blacklane&tags=%28story%2Ccomment%29&numericFilters=created_at_i%3E{SINCE_TS}&hitsPerPage=50")
    for request in api.requests:
        assert request.method == "GET"
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "OpenBerry-test"  # the shared client's headers are kept
        assert request.extensions["timeout"]["read"] == hn.REQUEST_TIMEOUT_SECONDS


async def test_enabled_types_limit_queries(company):
    api = FakeAlgolia()
    signals, _ = await run(configure(company, enabled_types=["hiring"]), api)
    assert api.keys == [FINDER, HIRING_EA, HIRING_TRAVEL]
    assert {r.signal.type for r in signals} == {"hiring"}

    api = FakeAlgolia()
    signals, _ = await run(configure(company, enabled_types=["keyword_mention"]), api)
    assert api.keys == [CHAUFFEUR, CORP_TRAVEL]
    assert {r.signal.type for r in signals} == {"keyword_mention"}


# --------------------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------------------


async def test_maps_mentions_to_person_signals(company):
    signals, _ = await run(company, FakeAlgolia())
    found = by_id(signals)
    assert set(found) == ALL_IDS  # fuzzy, author-only, too-old and deleted hits are dropped

    ask = found["hn:45410001"]
    sig = ask.signal
    assert sig.type == "competitor_engagement"
    assert sig.source == "hackernews"
    assert sig.strength == 85  # competitor + "frustrated with" / "alternatives to"
    assert sig.title == "Ask HN: Alternatives to Blacklane for corporate travel in Dubai?"
    assert sig.url == "https://news.ycombinator.com/item?id=45410001"
    assert sig.occurred_at == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    assert sig.summary.startswith("We've been frustrated with Blacklane's pricing")
    assert "<p>" not in sig.summary and "&#x27;" not in sig.summary
    assert sig.raw["kind"] == "story" and sig.raw["points"] == 12 and sig.raw["num_comments"] == 9
    assert {"frustrated with", "looking for", "alternatives to"} <= set(sig.raw["intent_phrases"])
    assert ask.account == "" and ask.lead is not None
    assert ask.lead.full_name == "dxb_ops"
    assert ask.lead.profile_url == "https://news.ycombinator.com/user?id=dxb_ops"
    assert ask.lead.source == "hackernews"

    plain = found["hn:45410002"].signal
    assert (plain.type, plain.strength) == ("competitor_engagement", 50)
    assert plain.title == 'Mentioned Blacklane on HN: "Ask HN: Alternatives to Blacklane for corporate travel in Dubai?"'
    assert plain.summary == "We use Blacklane for our offsites, works fine."
    assert plain.raw["kind"] == "comment" and plain.raw["story_id"] == 45410001

    thread_only = found["hn:45410003"].signal  # comment in a thread about the competitor
    assert (thread_only.strength, thread_only.raw["matched_in"]) == (40, "context")
    assert thread_only.title == 'Commented on HN thread "Blacklane raises $50M Series E"'

    asks_for_help = found["hn:45410020"].signal
    assert (asks_for_help.type, asks_for_help.strength) == ("keyword_mention", 75)
    assert asks_for_help.summary.endswith("Our travel desk is overwhelmed.")  # inline <i> removed cleanly
    assert found["hn:45410020"].lead.full_name == "ea_sarah"

    long_one = found["hn:45410021"].signal  # plural "chauffeurs" far into a long comment
    assert len(long_one.summary) <= 500
    assert long_one.summary.startswith("…") and "chauffeurs" in long_one.summary
    assert found["hn:45410030"].signal.summary == "Corporate-travel policies at my company are a mess."
    for raw in signals:
        assert raw.signal.occurred_at.tzinfo is not None
        assert raw.signal.external_id.startswith("hn:")


async def test_maps_who_is_hiring_posts_to_account_signals(company):
    signals, _ = await run(company, FakeAlgolia())
    found = by_id(signals)

    acme = found["hn:hiring:45401001"]
    assert acme.lead is None
    assert (acme.account, acme.account_domain) == ("Acme Bank", "acmebank.example")  # greenhouse link ignored
    sig = acme.signal
    assert (sig.type, sig.source, sig.strength) == ("hiring", "hackernews", 60)
    assert sig.title == "Hiring: Executive Assistant to the CEO"
    assert sig.url == "https://news.ycombinator.com/item?id=45401001"
    assert sig.occurred_at == datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    assert sig.summary.startswith("Acme Bank (YC W20) | Executive Assistant to the CEO | Dubai, UAE")
    assert sig.raw["matched"] == ["Executive Assistant", "Travel"]  # found by both hiring searches
    assert sig.raw["thread_id"] == THREAD and sig.raw["author"] == "acmebank_ops"

    northwind = found["hn:hiring:45401003"]  # keyword only in the body, lever.co link is not their domain
    assert (northwind.account, northwind.account_domain) == ("Northwind Logistics", "")
    assert (northwind.signal.title, northwind.signal.strength) == ("Hiring: Executive Assistant", 40)

    globex = found["hn:hiring:45401010"]
    assert (globex.account, globex.account_domain) == ("Globex", "globex.example")
    assert globex.signal.title == "Hiring: Travel Manager"
    # Replies (45401002, 45101008), deleted posts (45401005), posts without a "Company | Role"
    # headline (45401006) and prefix-only matches ("traveling", 45401011) are not hiring signals.
    assert not {"hn:hiring:45401002", "hn:hiring:45101008", "hn:hiring:45401005", "hn:hiring:45401006",
                "hn:hiring:45401011"} & set(found)

    # A late post in last month's thread, still inside the scan window. careers.<domain> is reduced
    # to the company's own domain.
    umbrella = found["hn:hiring:45101007"]
    assert (umbrella.account, umbrella.account_domain) == ("Umbrella Travel Group", "umbrella-travel.example")
    assert umbrella.signal.title == "Hiring: Corporate Travel Manager"
    assert umbrella.signal.raw["thread_id"] == PREV_THREAD
    assert umbrella.signal.raw["thread_title"] == "Ask HN: Who is hiring? (September 2026)"


async def test_picks_latest_who_is_hiring_thread(company):
    stories = fixture("whoishiring_stories.json")
    stories["hits"].reverse()  # order must not matter
    api = FakeAlgolia({FINDER: httpx.Response(200, json=stories)})
    await run(company, api)
    assert api.keys[1] == HIRING_EA  # story_45400001 (October), not the September thread


async def test_missing_hiring_thread_warns_and_still_searches_mentions(company):
    stories = fixture("whoishiring_stories.json")
    stories["hits"] = [h for h in stories["hits"] if "Who is hiring" not in h["title"]]
    api = FakeAlgolia({FINDER: httpx.Response(200, json=stories)})
    signals, ctx = await run(company, api)
    assert api.keys == [FINDER, BLACKLANE, CHAUFFEUR, CAREEM, CORP_TRAVEL]
    assert any("Who is hiring" in w for w in ctx.warnings)
    assert signals and all(r.signal.type != "hiring" for r in signals)


# --------------------------------------------------------------------------------------
# since, dedupe, caps
# --------------------------------------------------------------------------------------


async def test_only_items_newer_than_since(company):
    since = datetime(2026, 10, 2, tzinfo=timezone.utc)
    api = FakeAlgolia()  # the fake ignores numericFilters, so the collector must filter too
    signals, _ = await run(company, api, since=since)
    expected = f"created_at_i>{int(since.timestamp())}"
    assert "numericFilters" not in api.requests[0].url.params  # the thread finder is not time-filtered
    assert [r.url.params["numericFilters"] for r in api.requests[1:]] == [expected] * (len(api.requests) - 1)
    found = by_id(signals)
    # The October thread predates `since`, so September's thread is no longer searched.
    assert [tags for tags, _ in api.keys[1:3]] == [LATEST_ONLY_TAGS, LATEST_ONLY_TAGS]
    assert "hn:hiring:45401001" not in found and "hn:hiring:45401003" not in found  # Oct 1
    assert "hn:hiring:45101007" not in found  # previous thread
    assert "hn:hiring:45401010" in found  # Oct 2 08:00
    assert "hn:45410008" in found  # Oct 2 07:00
    assert all(r.signal.occurred_at > since for r in signals)


async def test_duplicates_merge_and_ids_are_stable(company):
    first, _ = await run(company, FakeAlgolia())
    second, _ = await run(company, FakeAlgolia())
    ids = [r.signal.external_id for r in first]
    assert len(ids) == len(set(ids))
    assert ids == [r.signal.external_id for r in second]

    # The Ask HN story is returned by the Blacklane, chauffeur and corporate travel searches.
    ask = by_id(first)["hn:45410001"].signal
    assert ask.type == "competitor_engagement"  # the stronger type wins
    assert ask.raw["query"] == "Blacklane"
    assert ask.raw["also_matched"] == ["chauffeur", "corporate travel"]

    # A keyword hit that a competitor search also found keeps the competitor type and the best strength.
    press = by_id(first)["hn:45410008"].signal
    assert (press.type, press.strength) == ("competitor_engagement", 50)


async def test_request_cap_and_daily_rotation(company):
    many = [f"topic {i}" for i in range(20)]
    company = configure(company, competitors=[], keywords=many, hiring_keywords=[])
    api = FakeAlgolia()
    _, ctx = await run(company, api)
    assert len(api.requests) == hn.MAX_REQUESTS_PER_SCAN == 12
    queried = [q for _, q in api.keys]
    assert len(set(queried)) == 12 and set(queried) <= set(many)
    assert any("only 12 searches" in w for w in ctx.warnings)

    # The searched subset rotates with the scan window so every term gets its turn.
    api_next_day = FakeAlgolia()
    await run(company, api_next_day, since=datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert [q for _, q in api_next_day.keys] != queried


async def test_hiring_keywords_are_capped_within_the_budget(company):
    company = configure(company, hiring_keywords=[f"Role {i}" for i in range(6)],
                        keywords=[f"kw {i}" for i in range(10)])
    api = FakeAlgolia()
    _, ctx = await run(company, api)
    assert len(api.requests) == 12
    hiring_queries = [q for tags, q in api.keys if tags == HIRING_TAGS]
    assert hiring_queries == ["Role 0", "Role 1", "Role 2", "Role 3"]
    assert any("first 4 of 6 hiring keywords" in w for w in ctx.warnings)


async def test_max_items_stops_collecting_and_requesting(company):
    api = FakeAlgolia()
    signals, _ = await run(company, api, max_items=3)
    assert len(signals) == 3
    assert api.keys == [FINDER, HIRING_EA, HIRING_TRAVEL]  # no mention searches once full

    api = FakeAlgolia()
    signals, _ = await run(configure(company, hiring_keywords=[]), api, max_items=2)
    assert len(signals) == 2
    assert api.keys == [BLACKLANE]


# --------------------------------------------------------------------------------------
# Failures never raise
# --------------------------------------------------------------------------------------

BLOCKED_HTML = "<html><head><title>403 Forbidden</title></head><body><h1>Forbidden</h1></body></html>"


async def test_403_html_stops_with_warning(company):
    api = FakeAlgolia(default=httpx.Response(403, text=BLOCKED_HTML, headers={"Content-Type": "text/html"}))
    signals, ctx = await run(company, api)
    assert signals == []
    assert len(api.requests) == 1
    assert any("403" in w for w in ctx.warnings)


async def test_429_stops_remaining_requests(company):
    api = FakeAlgolia({BLACKLANE: httpx.Response(429, json={"message": "Too many requests"})})
    signals, ctx = await run(company, api)
    assert api.keys == [FINDER, HIRING_EA, HIRING_TRAVEL, BLACKLANE]
    assert {r.signal.type for r in signals} == {"hiring"}  # what was fetched before is kept
    assert any("429" in w for w in ctx.warnings)


async def test_rate_limit_header_stops_after_this_response(company):
    page = fixture("search_blacklane.json")
    api = FakeAlgolia({BLACKLANE: httpx.Response(200, json=page, headers={"X-RateLimit-Remaining": "0"})})
    signals, ctx = await run(configure(company, hiring_keywords=[]), api)
    assert api.keys == [BLACKLANE]
    assert "hn:45410001" in by_id(signals)
    assert any("rate-limit" in w for w in ctx.warnings)


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


async def test_three_failures_in_a_row_stop_the_scan(company):
    api = FakeAlgolia({
        BLACKLANE: httpx.Response(500, text="Internal Server Error"),
        CHAUFFEUR: httpx.Response(200, content=b'{"hits": [', headers={"Content-Type": "application/json"}),
        CAREEM: _timeout,
    })
    signals, ctx = await run(configure(company, hiring_keywords=[]), api)
    assert api.keys == [BLACKLANE, CHAUFFEUR, CAREEM]  # "corporate travel" is never requested
    assert any("HTTP 500" in w for w in ctx.warnings)
    assert any("invalid JSON" in w for w in ctx.warnings)
    assert any("timed out" in w for w in ctx.warnings)
    assert any("3 failed requests in a row" in w for w in ctx.warnings)
    assert signals == []


async def test_failure_then_success_keeps_going(company):
    api = FakeAlgolia({
        BLACKLANE: httpx.Response(502, text="<html>Bad gateway</html>"),
        CHAUFFEUR: _timeout,
        CORP_TRAVEL: httpx.Response(200, content=b"not json at all"),
    })
    signals, ctx = await run(configure(company, hiring_keywords=[]), api)
    assert api.keys == [BLACKLANE, CHAUFFEUR, CAREEM, CORP_TRAVEL]  # CAREEM succeeded and reset the count
    assert len(ctx.warnings) == 3
    assert signals == []


async def test_unexpected_payload_message_is_reported(company):
    api = FakeAlgolia({CORP_TRAVEL: httpx.Response(200, json={"message": "you can only fetch the 1000 hits"})})
    signals, ctx = await run(configure(company, hiring_keywords=[]), api)
    assert any("1000 hits" in w for w in ctx.warnings)
    assert "hn:45410001" in by_id(signals)


async def test_connection_errors_never_raise(company):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    signals, ctx = await run(company, FakeAlgolia(default=refuse))
    assert signals == []
    assert len(ctx.warnings) == 4  # 3 failures + "stopped" notice
    assert any("ConnectError" in w for w in ctx.warnings)


async def test_bad_hits_are_skipped_not_fatal(company):
    page = fixture("search_blacklane.json")
    page["hits"] = [
        "not a dict",
        {"objectID": "1", "author": "a", "created_at_i": "garbage", "comment_text": "Blacklane!"},
        {"objectID": "2", "author": "b", "created_at_i": 1790900000, "comment_text": None, "story_title": None},
        {"author": "c", "created_at_i": 1790900000, "comment_text": "Blacklane rocks"},  # no objectID
        {"objectID": "3", "author": "d", "created_at_i": 1790900000, "comment_text": "Blacklane", "dead": True},
        {"objectID": "4", "author": "e", "created_at_i": 1790900000, "title": "[deleted]", "_tags": ["story"]},
        *page["hits"],
    ]
    api = FakeAlgolia({BLACKLANE: httpx.Response(200, json=page)})
    signals, _ = await run(configure(company, hiring_keywords=[]), api)
    found = by_id(signals)
    assert "hn:45410001" in found
    assert not {"hn:1", "hn:2", "hn:3", "hn:4"} & set(found)


async def test_malformed_hits_warn_once_per_query(company):
    page = fixture("search_blacklane.json")
    page["hits"] = [{"objectID": str(i), "author": "a", "created_at_i": 1790900000, "_tags": 5,
                     "title": "Blacklane"} for i in (1, 2)] + page["hits"]
    api = FakeAlgolia({BLACKLANE: httpx.Response(200, json=page)})
    signals, ctx = await run(configure(company, hiring_keywords=[]), api)
    assert "hn:45410001" in by_id(signals)
    assert ctx.warnings == ["Hacker News: skipped 2 malformed result(s) for 'Blacklane' (TypeError)"]


# --------------------------------------------------------------------------------------
# Review fixes: semantics, time handling, time budget
# --------------------------------------------------------------------------------------


async def test_job_thread_comments_are_not_mentions(company):
    signals, _ = await run(company, FakeAlgolia())
    found = by_id(signals)
    # A CV in "Who wants to be hired?" and a job ad in "Who is hiring?" both contain a keyword,
    # but neither is the author's buying intent.
    assert "hn:45400777" not in found and "hn:45401020" not in found
    seeker = fixture("search_corporate_travel.json")["hits"][1]
    assert seeker["story_title"].startswith("Ask HN: Who wants to be hired?")
    assert hn.mention_signal(seeker, "corporate travel", "keyword_mention", SINCE) is None
    freelancer = {**seeker, "story_title": "Ask HN: Freelancer? Seeking freelancer? (October 2026)"}
    assert hn.mention_signal(freelancer, "corporate travel", "keyword_mention", SINCE) is None
    ordinary = {**seeker, "story_title": "Ask HN: How do you manage corporate travel?"}
    assert hn.mention_signal(ordinary, "corporate travel", "keyword_mention", SINCE) is not None


async def test_naive_since_is_treated_as_utc(company):
    aware, _ = await run(company, FakeAlgolia())
    api = FakeAlgolia()
    naive, ctx = await run(company, api, since=SINCE.replace(tzinfo=None))
    assert ctx.warnings == []
    assert [r.signal.external_id for r in naive] == [r.signal.external_id for r in aware]
    assert api.requests[1].url.params["numericFilters"] == f"created_at_i>{SINCE_TS}"


async def test_time_budget_stops_new_requests(company):
    ticks = iter(range(0, 10_000, 25))  # every clock reading is 25 s later than the previous one
    collector = HackerNewsCollector()
    collector.time_budget = 60
    collector.clock = lambda: float(next(ticks))
    api = FakeAlgolia()
    signals, ctx = await run(company, api, collector=collector)
    assert api.keys == [FINDER, HIRING_EA]  # third request would start at t=75 s > 60 s budget
    assert any("time budget" in w for w in ctx.warnings)
    assert {r.signal.external_id for r in signals} == {"hn:hiring:45401001", "hn:hiring:45401003"}


async def test_request_timeout_is_capped_below_the_collector_timeout(company):
    slow = dataclasses.replace(get_settings(), http_timeout=90.0)
    api = FakeAlgolia()
    await run(company, api, settings=slow)
    assert {r.extensions["timeout"]["read"] for r in api.requests} == {hn.REQUEST_TIMEOUT_SECONDS}
    fast = dataclasses.replace(get_settings(), http_timeout=5.0)
    api = FakeAlgolia()
    await run(company, api, settings=fast)
    assert {r.extensions["timeout"]["read"] for r in api.requests} == {5.0}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("headline", "keyword", "expected"), [
    ("Acme (YC S19) | Executive Assistant | Dubai | Onsite", "Executive Assistant", ("Acme", "Executive Assistant")),
    ("Acme | Remote | Senior Travel Manager | $120k", "Travel", ("Acme", "Senior Travel Manager")),
    ("Acme | Backend Engineer | Remote", "Executive Assistant", ("Acme", "Executive Assistant")),
    ("Globex - Travel Coordinator - Berlin", "Travel", ("Globex", "Travel Coordinator")),
    ("https://acme.example | Travel Manager", "Travel", ("acme.example", "Travel Manager")),
    ("We are hiring travel people, email me", "Travel", None),
    (" | Travel Manager", "Travel", None),
])
def test_parse_headline(headline, keyword, expected):
    assert hn.parse_headline(headline, keyword) == expected


def test_company_domain_skips_job_boards():
    html = ('Acme | EA | <a href="https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;acme">x</a> '
            '<a href="https:&#x2F;&#x2F;www.acme.example&#x2F;jobs">acme</a>')
    assert hn.company_domain(html, "Acme") == "acme.example"
    assert hn.company_domain("Acme | EA | https://boards.greenhouse.io/acme", "Acme") == ""
    assert hn.company_domain("Acme | EA | Remote", "Acme") == ""


@pytest.mark.parametrize(("headline", "company", "expected"), [
    # Multi-tenant job hosts must never become the account domain: every company using them
    # would share the identity key acct:d:<host> and merge into one account lead.
    ("Initech | Travel Manager | https://jobs.gem.com/initech", "Initech", ""),
    ("Hooli | EA | https://hooli.wd5.myworkdayjobs.com/en-US/careers", "Hooli", ""),
    ("Hooli | EA | https://some-ats.example/hooli/apply", "Hooli", ""),  # unknown host, no resemblance
    ("Hooli | EA | https://careers.hooli.com/jobs/1", "Hooli", "hooli.com"),
    ("Acme Bank (YC W20) | EA | https://acme.io", "Acme Bank", "acme.io"),
    ("Acme | EA | https://getacme.com/careers", "Acme", "getacme.com"),
    ("IBM | EA | https://www.ibm.com", "IBM", "ibm.com"),
    ("acme.example | Travel Manager", "acme.example", "acme.example"),  # bare-domain company name
])
def test_company_domain_requires_resemblance(headline, company, expected):
    assert hn.company_domain(headline, company) == expected


def test_hiring_threads_cover_the_scan_window():
    stories = fixture("whoishiring_stories.json")["hits"]
    early = hn.hiring_threads(stories, datetime(2026, 9, 24, tzinfo=timezone.utc))
    assert [t.id for t in early] == [THREAD, PREV_THREAD]  # window reaches back into September
    late = hn.hiring_threads(stories, datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert [t.id for t in late] == [THREAD]
    assert hn.hiring_threads([s for s in stories if "hiring?" not in s["title"]], SINCE) == []


def test_hiring_headline_after_a_leading_paragraph_tag():
    threads = {THREAD: hn.HiringThread(id=THREAD, title="Ask HN: Who is hiring? (October 2026)")}
    hit = {"objectID": "9", "author": "x", "parent_id": int(THREAD), "created_at_i": 1790900000,
           "comment_text": "<p>Hooli | Travel Manager | NYC<p>Run our travel desk."}
    raw = hn.hiring_signal(hit, "Travel Manager", threads, SINCE)
    assert raw is not None
    assert (raw.account, raw.signal.title, raw.signal.strength) == ("Hooli", "Hiring: Travel Manager", 60)


@pytest.mark.parametrize(("args", "expected"), [
    (("keyword_mention", "text", False, False, False), 50),
    (("keyword_mention", "text", False, False, True), 60),
    (("keyword_mention", "text", True, False, False), 75),
    (("keyword_mention", "text", True, False, True), 80),
    (("competitor_engagement", "text", True, True, False), 85),
    (("competitor_engagement", "context", False, False, False), 40),
    (("keyword_mention", "context", False, False, False), 30),
    (("keyword_mention", "context", True, False, False), 60),
])
def test_mention_strength_rules(args, expected):
    assert hn.mention_strength(*args) == expected


def test_term_pattern():
    pattern = hn.term_pattern("corporate travel")
    assert pattern.search("Corporate-travel policies")
    assert pattern.search("two corporate  travels")
    assert not pattern.search("corporate traveling")
    assert not hn.term_pattern("CRM").search("crmble")


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------


async def test_ingest_creates_scored_leads(company):
    signals, _ = await run(company, FakeAlgolia())
    stats = ingest(company.id, signals)
    assert stats.errors == []
    assert stats.signals_new == len(signals) == len(ALL_IDS)

    accounts, _ = repo.list_leads(company.id, kind="account")
    acme = next(lead for lead in accounts if lead.lead_company == "Acme Bank")
    assert acme.company_domain == "acmebank.example" and acme.source == "hackernews"
    assert acme.intent_score > 0 and acme.score > 0

    people, _ = repo.list_leads(company.id, kind="person", source="hackernews")
    asker = next(lead for lead in people if lead.full_name == "dxb_ops")
    assert asker.profile_url == "https://news.ycombinator.com/user?id=dxb_ops"
    assert asker.intent_score > 0 and asker.score > 0 and asker.score_reasons
    lead_signals, _ = repo.list_signals(company.id, lead_id=asker.id)
    assert "hn:45410001" in {s.external_id for s in lead_signals}

    hn_signals, total = repo.list_signals(company.id, source="hackernews")
    assert total == len(ALL_IDS)
    assert {s.type for s in hn_signals} == {"hiring", "competitor_engagement", "keyword_mention"}

    again = ingest(company.id, (await run(company, FakeAlgolia()))[0])
    assert again.signals_new == 0 and again.signals_duplicate == len(ALL_IDS)


async def test_each_hn_author_is_a_distinct_lead(company):
    signals, _ = await run(company, FakeAlgolia())
    authors = {r.lead.full_name for r in signals if r.lead is not None}
    assert len(authors) == 7
    ingest(company.id, signals)
    people, total = repo.list_leads(company.id, kind="person")
    assert total == len(authors)
    assert {p.full_name for p in people} == authors
    again = ingest(company.id, signals)
    assert again.leads_new == 0  # the same HN user merges across scans
