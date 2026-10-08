"""Hacker News collector (Algolia HN Search API), fully offline via httpx.MockTransport.

Fixtures in tests/fixtures/hackernews mirror the recorded Algolia payloads from the API research
(search_by_date hits for stories and comments, and the monthly whoishiring threads).
"""

from __future__ import annotations

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
THREAD = "45400001"

FINDER = ("story,author_whoishiring", None)
HIRING_EA = (f"comment,story_{THREAD}", "Executive Assistant")
HIRING_TRAVEL = (f"comment,story_{THREAD}", "Travel")
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


async def run(company: Company, handler: FakeAlgolia, *, since: datetime = SINCE,
              max_items: int = 200) -> tuple[list[RawSignal], CollectContext]:
    collector = HackerNewsCollector()
    collector.request_interval = 0
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"User-Agent": "OpenBerry-test"}) as client:
        ctx = CollectContext(client=client, since=since, settings=get_settings(), max_items=max_items)
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
    "hn:hiring:45401001", "hn:hiring:45401003", "hn:hiring:45401010",
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
    assert params[1] == {"query": "Executive Assistant", "tags": f"comment,story_{THREAD}",
                         "numericFilters": f"created_at_i>{SINCE_TS}", "hitsPerPage": "100"}
    assert params[3] == {"query": "Blacklane", "tags": "(story,comment)",
                         "numericFilters": f"created_at_i>{SINCE_TS}", "hitsPerPage": "50"}
    assert str(api.requests[3].url) == (
        f"{SEARCH}?query=Blacklane&tags=%28story%2Ccomment%29&numericFilters=created_at_i%3E{SINCE_TS}&hitsPerPage=50")
    for request in api.requests:
        assert request.method == "GET"
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "OpenBerry-test"  # the shared client's headers are kept


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
    # Replies (45401002), deleted posts (45401005), posts without a "Company | Role" headline
    # (45401006) and prefix-only matches ("traveling", 45401011) are not hiring signals.
    assert not {"hn:hiring:45401002", "hn:hiring:45401005", "hn:hiring:45401006", "hn:hiring:45401011"} & set(found)


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
    assert "hn:hiring:45401001" not in found and "hn:hiring:45401003" not in found  # Oct 1
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
    hiring_queries = [q for tags, q in api.keys if tags == f"comment,story_{THREAD}"]
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
    assert hn.company_domain(html) == "acme.example"
    assert hn.company_domain("Acme | EA | https://boards.greenhouse.io/acme") == ""
    assert hn.company_domain("Acme | EA | Remote") == ""


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


_HN_IDS_COLLAPSE = (repo.lead_identity_keys(hn.hn_lead("alice"), "person")
                    == repo.lead_identity_keys(hn.hn_lead("bob"), "person"))


@pytest.mark.xfail(_HN_IDS_COLLAPSE, strict=True,
                   reason="repo.lead_identity_keys drops the ?id= query of HN profile URLs, so every HN user "
                          "shares one identity key (core change requested)")
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
