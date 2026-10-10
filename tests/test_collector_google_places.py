"""Google Maps businesses collector, fully offline: Google's Text Search (New) and the businesses' websites are
httpx.MockTransports (tests/places_fakes.py) and real DNS lookups fail. Google is never called."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from openberry import db, repo, services
from openberry.collectors import COLLECTORS, google_places
from openberry.collectors.base import CollectContext, RawSignal
from openberry.collectors.google_places import (
    FIELD_MASK,
    SEARCH_URL,
    GooglePlacesCollector,
    PlacesError,
    check_key,
    due_queries,
    parse_search_response,
    query_location,
    usage_summary,
)
from openberry.config import get_settings
from openberry.models import Company
from places_fakes import (
    ACME,
    CLOUDFLARE,
    DESERT,
    DOWN,
    FACEBOOK,
    GOOGLE_KEY,
    GULF,
    JUNK,
    MARKERS,
    PALMCREST,
    NO_SITE,
    PRIVATE_CLUB,
    QUERY,
    TOKEN2,
    FakeGoogle,
    FakeSites,
    go_offline,
    google_error,
    google_json,
    search_page,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=14)
ADDED = [ACME, DESERT, GULF, PALMCREST, CLOUDFLARE, JUNK]


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch, settings) -> FakeSites:
    fake = FakeSites()
    go_offline(monkeypatch, fake)
    monkeypatch.setattr(google_places, "_utcnow", lambda: NOW)
    monkeypatch.setattr(repo, "utcnow", lambda: NOW)
    monkeypatch.setattr(settings, "google_places_key", GOOGLE_KEY)
    return fake


@pytest.fixture
def maps_company(company, sites) -> Company:
    return repo.update_company(company.id, {"signals": {"places_queries": [QUERY]}})


def configure(company: Company, **signals: Any) -> Company:
    return company.model_copy(update={"signals": company.signals.model_validate(
        {**company.signals.model_dump(), **signals})})


async def run(company: Company, google: FakeGoogle | None = None, *, collector: GooglePlacesCollector | None = None,
              max_items: int = 200) -> tuple[list[RawSignal], CollectContext]:
    google = google or FakeGoogle()
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        ctx = CollectContext(client=client, since=SINCE, settings=get_settings(), max_items=max_items)
        signals = await (collector or GooglePlacesCollector()).collect(company, ctx)
    return signals, ctx


def by_place(signals: list[RawSignal]) -> dict[str, RawSignal]:
    return {r.signal.external_id.removeprefix("gp:"): r for r in signals}


def set_usage(used: int) -> None:
    with db.connect() as c:
        c.execute("INSERT INTO api_usage (service, month, used, updated_at) VALUES (?, ?, ?, ?)",
                  (google_places.SERVICE, repo.usage_month(NOW), used, repo.iso(NOW)))


def used_this_month() -> int:
    return repo.api_usage(google_places.SERVICE, 900, now=NOW).used


def many_pages(queries: list[str]) -> dict[tuple[str, str], Any]:
    """Every search has a next page, forever: only the caps stop it."""
    routes: dict[tuple[str, str], Any] = {}
    for q in queries:
        for n in range(4):
            token = f"tok{n}-{abs(hash(q))}"
            routes[(q, "" if n == 0 else f"tok{n - 1}-{abs(hash(q))}")] = (
                lambda r, token=token: search_page([], token))
    return routes


# --------------------------------------------------------------------------------------
# Registration and configuration
# --------------------------------------------------------------------------------------


def test_registered_with_declared_metadata():
    collector = COLLECTORS["google_places"]
    assert isinstance(collector, GooglePlacesCollector)
    assert (collector.label, collector.signal_types) == ("Google Maps businesses", ("business_search",))
    assert collector.timeout_seconds == 300 and collector.time_budget < collector.timeout_seconds
    assert "SECRET" not in collector.requires.upper() and "OPENBERRY_" not in collector.requires


def test_configured_with_a_key_and_a_search(company, settings, monkeypatch):
    collector = GooglePlacesCollector()
    with_search = configure(company, places_queries=[QUERY])
    assert not collector.is_configured(with_search)  # no key
    monkeypatch.setattr(settings, "google_places_key", GOOGLE_KEY)
    assert collector.is_configured(with_search)
    assert not collector.is_configured(company)  # no search
    # The searches are the opt-in: "Signal types to track" doesn't offer business_search.
    assert collector.enabled_for(configure(with_search, enabled_types=["hiring"]))


# --------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------


async def test_request_shape_and_paging(maps_company):
    google = FakeGoogle()
    await run(maps_company, google)

    assert len(google.requests) == 3
    first = google.requests[0]
    assert first.method == "POST" and str(first.url) == SEARCH_URL
    assert first.headers["x-goog-api-key"] == GOOGLE_KEY
    assert first.headers["x-goog-fieldmask"] == FIELD_MASK == "places.id,places.websiteUri,nextPageToken"
    assert first.headers["content-type"] == "application/json"
    assert all(GOOGLE_KEY not in str(r.url) for r in google.requests)
    bodies = google.bodies
    assert bodies[0] == {"textQuery": QUERY, "pageSize": 20, "includePureServiceAreaBusinesses": True}
    assert bodies[1] == {**bodies[0], "pageToken": TOKEN2}  # every other parameter repeated exactly
    assert bodies[2]["pageToken"] == "AeCrKXsPAGETHREE-token_value"


async def test_one_scan_finds_the_businesses_with_their_own_contact_details(maps_company, sites):
    signals, ctx = await run(maps_company)

    assert list(by_place(signals)) == ADDED  # Google's order
    assert ctx.counts == {"searches": 3, "businesses": 11, "added": 6, "no_website": 2, "unreachable": 1,
                          "robots": 1}
    assert ctx.warnings == []
    acme = by_place(signals)[ACME]
    maps = f"https://www.google.com/maps/place/?q=place_id:{ACME}"
    sig, lead = acme.signal, acme.lead
    assert (sig.type, sig.source, sig.external_id, sig.strength) == ("business_search", "google_places",
                                                                     f"gp:{ACME}", 10)
    assert sig.title == f"Found on Google Maps: “{QUERY}”" and sig.url == maps and sig.occurred_at == NOW
    assert sig.summary == (f"Acme Events came up in your Google Maps search “{QUERY}”. Its website lists "
                           "info@acme-events.ae and +97145550101.")
    assert lead.model_dump(include={"lead_company", "company_domain", "website", "email", "phone", "location",
                                    "profile_url", "source", "full_name"}) == {
        "lead_company": "Acme Events", "company_domain": "acme-events.ae", "website": "https://www.acme-events.ae/",
        "email": "info@acme-events.ae", "phone": "+97145550101", "location": "Dubai", "profile_url": maps,
        "source": "google_places", "full_name": ""}
    assert lead.notes == "Other contacts on its website: events@acme-events.ae, jobs@acme-events.ae, +971505550102"
    assert lead.bio.startswith("Acme Events plans corporate events")
    hotel = by_place(signals)[PALMCREST].lead
    assert hotel.company_domain == "" and hotel.lead_company == "Palmcrest Marina Hotel Dubai"
    cf = by_place(signals)[CLOUDFLARE].signal
    assert cf.summary.endswith("Its website lists +97145550188.")
    # No website, or only a social page: never fetched.
    assert not sites.hits("www.facebook.com")
    # Skipped Place IDs are remembered, the search is marked read. Added ones are recorded only when the scan
    # stores their leads (services.ingest).
    assert repo.handled_place_ids(maps_company.id, [ACME, NO_SITE, FACEBOOK, DOWN, PRIVATE_CLUB],
                                  NOW - timedelta(days=30)) == {NO_SITE, FACEBOOK, DOWN, PRIVATE_CLUB}
    assert repo.place_search_times(maps_company.id) == {QUERY.casefold(): NOW}
    services.ingest(maps_company.id, signals)
    assert repo.handled_place_ids(maps_company.id, ADDED, NOW - timedelta(days=400)) == set(ADDED)


async def test_a_scan_stopped_before_storing_its_leads_loses_no_business(maps_company, monkeypatch):
    async def never(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return search_page([])

    # Page 1 is read (Acme Events and Desert DMC become signals), then the collector times out on page 2: the
    # signals it held are never stored, like a desktop app closed during a scan.
    monkeypatch.setattr(GooglePlacesCollector, "timeout_seconds", 0.5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle({(QUERY, TOKEN2): never}))) as client:
        stats = await services.run_scan(maps_company.id, sources=["google_places"], client=client)
    assert stats["leads_new"] == 0 and "TimeoutError" in stats["collectors"]["google_places"]["error"]
    monkeypatch.setattr(GooglePlacesCollector, "timeout_seconds", google_places.TIMEOUT_SECONDS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle())) as client:
        stats = await services.run_scan(maps_company.id, sources=["google_places"], client=client)
    assert stats["leads_new"] == 6
    names = {lead.lead_company for lead in repo.list_leads(maps_company.id, limit=50)[0]}
    assert {"Acme Events", "Desert DMC"} <= names


async def test_a_lead_that_could_not_be_stored_comes_back(maps_company, monkeypatch):
    real = repo.upsert_lead

    def broken(company_id, data, **kwargs):
        if data.lead_company == "Acme Events":
            raise ValueError("disk full")
        return real(company_id, data, **kwargs)

    monkeypatch.setattr(repo, "upsert_lead", broken)
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle())) as client:
        stats = await services.run_scan(maps_company.id, sources=["google_places"], client=client)
    assert stats["leads_new"] == 5 and stats["errors"] == ["google_places: disk full"]
    assert repo.handled_place_ids(maps_company.id, [ACME, DESERT], NOW - timedelta(days=30)) == {DESERT}
    monkeypatch.setattr(repo, "upsert_lead", real)
    repo.finish_place_search(maps_company.id, QUERY.casefold(), 0, 0, when=NOW - timedelta(days=8))
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle())) as client:
        stats = await services.run_scan(maps_company.id, sources=["google_places"], client=client)
    assert stats["leads_new"] == 1
    assert any(lead.lead_company == "Acme Events" for lead in repo.list_leads(maps_company.id, limit=50)[0])


async def test_nothing_from_google_but_the_place_id_is_kept(maps_company, settings):
    google = FakeGoogle()
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        stats = await services.run_scan(maps_company.id, sources=["google_places"], client=client)
    assert stats["status"] == "ok" and stats["leads_new"] == 6

    conn = sqlite3.connect(settings.db_path)
    dump = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            dump.extend(str(v) for v in row)
    conn.close()
    text = "\n".join(dump)
    for marker in MARKERS:
        assert marker not in text, marker
    assert ACME in text and "utm_" not in text


async def test_the_last_page_ends_a_search_even_with_a_token(maps_company):
    google = FakeGoogle({(QUERY, "AeCrKXsPAGETHREE-token_value"): lambda r: search_page([], "PAGE-FOUR")})
    _, ctx = await run(maps_company, google)
    assert len(google.requests) == 3 and ctx.counts["searches"] == 3
    assert QUERY.casefold() in repo.place_search_times(maps_company.id)


async def test_per_scan_cap_starts_a_search_only_when_all_its_pages_fit(maps_company):
    queries = [f"hotels in area {n}" for n in range(5)]
    company = repo.update_company(maps_company.id, {"signals": {"places_queries": queries}})
    google = FakeGoogle(many_pages(queries))
    _, ctx = await run(company, google)
    assert len(google.requests) == 9  # 3 searches of 3 pages; a 4th wouldn't fit in 10
    assert [b["textQuery"] for b in google.bodies[::3]] == queries[:3]
    assert ctx.counts["searches_waiting"] == 2 and ctx.counts["searches"] == 9
    assert used_this_month() == 9
    # The next scan starts with the two that waited.
    google = FakeGoogle(many_pages(queries))
    await run(company, google)
    assert [b["textQuery"] for b in google.bodies[::3]] == queries[3:]


# --------------------------------------------------------------------------------------
# Monthly cap
# --------------------------------------------------------------------------------------


async def test_one_search_left_this_month(maps_company):
    set_usage(899)
    google = FakeGoogle()
    signals, ctx = await run(maps_company, google)
    assert len(google.requests) == 1 and len(signals) == 2  # page 1's two readable websites
    assert ctx.warnings == ["Google Maps: this month's limit of 900 searches is used up. Searches start again on "
                            "1 November."]
    assert used_this_month() == 900
    assert repo.place_search_times(maps_company.id) == {}  # not every page was read: it runs again


async def test_no_search_when_the_month_is_used_up(maps_company):
    set_usage(900)
    google = FakeGoogle()
    signals, ctx = await run(maps_company, google)
    assert google.requests == [] and signals == []
    assert "limit of 900 searches is used up" in ctx.warnings[0]


async def test_a_limit_of_zero_turns_the_searches_off(maps_company, settings, monkeypatch):
    monkeypatch.setattr(settings, "google_places_monthly_limit", 0)
    google = FakeGoogle()
    signals, ctx = await run(maps_company, google)
    assert google.requests == [] and signals == []
    assert ctx.warnings == [google_places.LIMIT_OFF]


async def test_failed_requests_are_counted_too(maps_company):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    company = repo.update_company(maps_company.id, {"signals": {"places_queries": [QUERY, "dmcs in Dubai"]}})
    google = FakeGoogle({(QUERY, ""): lambda r: google_json({"error": "x"}, 503), ("dmcs in Dubai", ""): timeout})
    signals, ctx = await run(company, google)
    assert len(google.requests) == 2 and used_this_month() == 2 and signals == []
    assert ctx.warnings == [f"Google Maps: the search “{QUERY}” failed (HTTP 503).",
                            "Google Maps: the search “dmcs in Dubai” failed (timed out).",
                            "Google Maps: 2 failed searches in a row; stopped for this scan."]


# --------------------------------------------------------------------------------------
# Google's errors
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "warning", "counted"), [
    (lambda r: google_error("error_api_key_invalid.json"),
     "Google Maps: Google says the API key is not valid. Check it on the API keys page.", 0),
    (lambda r: google_error("error_service_disabled.json"),
     "Google Maps: Places API (New) is off for this key's Google Cloud project. Turn it on, then scan again.", 0),
    (lambda r: google_error("error_billing_disabled.json"),
     "Google Maps: billing is off for this key's Google Cloud project. Google needs billing even for free searches.",
     0),
    (lambda r: httpx.Response(403, text="<html>Forbidden</html>"),
     "Google Maps: Google refused the key (HTTP 403). Allow Places API (New) in the key's restrictions.", 0),
    (lambda r: google_json({"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "blocked", "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_SERVICE_BLOCKED"}]}}, 403),
     "Google Maps: Google refused the key (API_KEY_SERVICE_BLOCKED). Allow Places API (New) in the key's "
     "restrictions.", 0),
    (lambda r: google_error("error_resource_exhausted.json"),
     "Google Maps: Google's limit for this key was reached (HTTP 429). Searches stopped for this scan.", 1),
])
async def test_errors_that_stop_the_collector(maps_company, answer, warning: str, counted: int):
    company = repo.update_company(maps_company.id, {"signals": {"places_queries": [QUERY, "dmcs in Dubai"]}})
    google = FakeGoogle({(QUERY, ""): answer})
    signals, ctx = await run(company, google)
    assert len(google.requests) == 1 and signals == []
    assert ctx.warnings == [warning]
    assert repo.place_search_times(company.id) == {}
    # Refused before the search ran (key, billing, API off): never billed, so not counted against the month.
    assert used_this_month() == counted


async def test_a_refused_key_never_uses_up_the_month(maps_company, settings, monkeypatch):
    monkeypatch.setattr(settings, "google_places_monthly_limit", 5)
    refused = FakeGoogle({(QUERY, ""): lambda r: google_error("error_billing_disabled.json")})
    for _ in range(6):
        await run(maps_company, refused)
    assert used_this_month() == 0
    signals, ctx = await run(maps_company)  # billing turned on
    assert len(signals) == 6 and ctx.warnings == [] and used_this_month() == 3


async def test_a_rejected_page_stops_only_that_search(maps_company):
    company = repo.update_company(maps_company.id, {"signals": {"places_queries": [QUERY, "dmcs in Dubai"]}})
    google = FakeGoogle({(QUERY, TOKEN2): lambda r: google_error("error_invalid_argument.json"),
                         ("dmcs in Dubai", ""): lambda r: search_page([{"id": "ChIJDmcOnly00000011"}])})
    signals, ctx = await run(company, google)
    assert [b["textQuery"] for b in google.bodies] == [QUERY, QUERY, "dmcs in Dubai"]
    assert ctx.warnings == [f"Google Maps: Google rejected the search “{QUERY}” (INVALID_ARGUMENT: Request contains "
                            "an invalid argument.)."]
    assert len(signals) == 2  # page 1 still counts
    assert set(repo.place_search_times(company.id)) == {"dmcs in dubai"}  # the interrupted one runs again


async def test_unreadable_answers_count_as_failures(maps_company):
    google = FakeGoogle({(QUERY, ""): lambda r: httpx.Response(200, text="<html>captive portal</html>")})
    _, ctx = await run(maps_company, google)
    assert ctx.warnings == [f"Google Maps: the search “{QUERY}” failed (Google's answer was not readable)."]


async def test_no_warning_ever_contains_the_key(maps_company):
    echo = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": f"Bad key {GOOGLE_KEY} here"}}
    google = FakeGoogle({(QUERY, ""): lambda r: google_json(echo, 400)})
    _, ctx = await run(maps_company, google)
    assert ctx.warnings and all(GOOGLE_KEY not in w for w in ctx.warnings)


def test_parse_search_response():
    page = parse_search_response({"places": [
        {"id": ACME, "websiteUri": " https://acme.ae/ "}, {"id": ACME, "websiteUri": "https://dup.ae/"},
        {"id": "x"}, {"id": 12345678901}, {"websiteUri": "https://no-id.ae/"}, "junk",
        {"id": DESERT, "websiteUri": "javascript:alert(1)"}, {"id": GULF, "displayName": {"text": "ignored"}},
    ], "nextPageToken": "next"})
    assert page.places == [(ACME, "https://acme.ae/"), (DESERT, ""), (GULF, "")]
    assert page.next_page_token == "next"
    assert parse_search_response({}).places == [] and parse_search_response({}).next_page_token == ""
    with pytest.raises(ValueError):
        parse_search_response([])


# --------------------------------------------------------------------------------------
# What runs, and what is skipped
# --------------------------------------------------------------------------------------


def test_due_queries():
    last = {"a": NOW - timedelta(days=3), "b": NOW - timedelta(days=8), "c": NOW - timedelta(days=20)}
    assert due_queries(["A", "B", "new one", "C", "another"], last, NOW) == ["new one", "another", "C", "B"]


async def test_a_search_read_this_week_waits(maps_company):
    repo.finish_place_search(maps_company.id, QUERY.casefold(), 11, 6, when=NOW - timedelta(days=3))
    google = FakeGoogle()
    signals, ctx = await run(maps_company, google)
    assert google.requests == [] and signals == [] and ctx.counts == {"searches_not_due": 1}
    repo.finish_place_search(maps_company.id, QUERY.casefold(), 11, 6, when=NOW - timedelta(days=8))
    await run(maps_company, google)
    assert len(google.requests) == 3


async def test_removed_searches_are_forgotten(maps_company):
    repo.finish_place_search(maps_company.id, "old search", 3, 1, when=NOW)
    await run(maps_company)
    assert set(repo.place_search_times(maps_company.id)) == {QUERY.casefold()}


async def test_handled_place_ids_are_not_visited_again(maps_company, sites):
    repo.record_place_ids(maps_company.id, {ACME: True}, when=NOW - timedelta(days=400))
    repo.record_place_ids(maps_company.id, {DESERT: False}, when=NOW - timedelta(days=10))
    repo.record_place_ids(maps_company.id, {GULF: False}, when=NOW - timedelta(days=31))
    signals, ctx = await run(maps_company)
    assert ACME not in by_place(signals) and DESERT not in by_place(signals) and GULF in by_place(signals)
    assert ctx.counts["already_handled"] == 2
    assert not sites.hits("www.acme-events.ae") and not sites.hits("desertdmc.com")


async def test_a_deleted_lead_does_not_come_back(maps_company):
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle())) as client:
        await services.run_scan(maps_company.id, sources=["google_places"], client=client)
    acme = next(lead for lead in repo.list_leads(maps_company.id, limit=50)[0] if lead.lead_company == "Acme Events")
    repo.delete_lead(acme.id)
    repo.finish_place_search(maps_company.id, QUERY.casefold(), 0, 0, when=NOW - timedelta(days=8))
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle())) as client:
        stats = await services.run_scan(maps_company.id, sources=["google_places"], client=client)
    assert stats["leads_new"] == 0
    assert not any(lead.lead_company == "Acme Events" for lead in repo.list_leads(maps_company.id, limit=50)[0])


async def test_own_site_competitors_and_never_contact_companies_are_excluded(maps_company):
    company = repo.update_company(maps_company.id, {
        "website": "acme-events.ae", "competitors": ["Blacklane", "Gulf Law Partners"],
        "icp": {"exclude_companies": ["desertdmc.com", "Junk Free"]}})
    signals, ctx = await run(company)
    assert list(by_place(signals)) == [PALMCREST, CLOUDFLARE]
    assert ctx.counts["excluded"] == 4
    assert repo.handled_place_ids(company.id, [ACME, JUNK], NOW - timedelta(days=30)) == {ACME, JUNK}  # added = 0
    with db.connect() as c:
        assert c.execute("SELECT added FROM place_ids WHERE place_id = ?", (ACME,)).fetchone()[0] == 0


@pytest.mark.parametrize(("query", "icp", "location"), [
    ("event management companies in Dubai", [], "Dubai"),
    ("hotels in Business Bay, Dubai", [], "Business Bay, Dubai"),
    ("law firms near DIFC", [], "DIFC"),
    ("corporate offices in the UAE.", [], "the UAE"),
    ("banks Abu Dhabi", ["UAE", "Qatar"], "UAE"),
    ("printing shops", ["UAE"], ""),
])
def test_query_location(query: str, icp: list[str], location: str):
    assert query_location(query, icp) == location


async def test_max_items_and_the_time_budget(maps_company):
    signals, _ = await run(maps_company, max_items=2)
    assert len(signals) == 2

    slow = GooglePlacesCollector()
    slow.time_budget = 30  # under the minute a page needs
    google = FakeGoogle()
    signals, ctx = await run(maps_company, google, collector=slow)
    assert google.requests == [] and ctx.counts == {"searches_waiting": 1}


async def test_businesses_whose_turn_comes_too_late_are_not_checked(maps_company, monkeypatch, sites):
    monkeypatch.setattr(google_places, "MIN_SECONDS_FOR_A_SITE", google_places.MIN_SECONDS_FOR_A_PAGE + 5)
    collector = GooglePlacesCollector()
    collector.time_budget = google_places.MIN_SECONDS_FOR_A_PAGE + 1
    signals, ctx = await run(maps_company, collector=collector)
    assert signals == [] and ctx.counts["not_checked"] == 9 and ctx.counts["no_website"] == 1
    assert ctx.warnings == ["Google Maps: ran out of time; 9 businesses were not checked. They come back on the "
                            "next search."]
    assert repo.handled_place_ids(maps_company.id, [ACME, DESERT], NOW - timedelta(days=30)) == set()
    assert sites.requests == []


# --------------------------------------------------------------------------------------
# Usage summary and the free key check
# --------------------------------------------------------------------------------------


def test_usage_summary_never_contains_the_key(sites, settings):
    set_usage(23)
    summary = usage_summary()
    assert summary == {"searches_used_this_month": 23, "monthly_limit": 900, "month": "2026-10",
                       "resets_on": "2026-11-01", "api_key_set": True, "searches_per_scan": 10}
    assert GOOGLE_KEY not in json.dumps(summary)
    assert GooglePlacesCollector().usage() == summary
    assert google_places.first_of_next_month(datetime(2026, 12, 31, 23, tzinfo=timezone.utc)).isoformat() == \
        "2027-01-01"
    assert google_places.day_month("2027-01-01") == "1 January"


@pytest.mark.parametrize(("answer", "expected"), [
    (lambda r: google_json({"places": [{"id": ACME}]}), (True, "")),
    (lambda r: google_error("error_api_key_invalid.json"),
     (False, "Google says the API key is not valid. Check it on the API keys page.")),
    (lambda r: google_error("error_service_disabled.json"),
     (False, "Places API (New) is off for this key's Google Cloud project. Turn it on, then scan again.")),
    (lambda r: httpx.Response(403, text="Forbidden"),
     (False, "Google refused the key (HTTP 403). Allow Places API (New) in the key's restrictions.")),
    (lambda r: google_json({}, 503), (None, "HTTP 503")),
])
async def test_check_key(sites, answer, expected):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answer(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await check_key(GOOGLE_KEY, client) == expected
    assert requests[0].headers["x-goog-fieldmask"] == "places.id"  # IDs only: no charge
    assert json.loads(requests[0].content) == {"textQuery": "hotel", "pageSize": 1,
                                               "includePureServiceAreaBusinesses": True}
    assert used_this_month() == 0  # not counted


async def test_check_key_when_google_cannot_be_reached(sites):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        assert await check_key(GOOGLE_KEY, client) == (None, "ConnectError")


def test_places_error_messages_are_short():
    resp = httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "message": "x" * 1000}})
    error = google_places.places_error(resp, "q")
    assert isinstance(error, PlacesError) and error.kind == "bad_request" and len(error.message) < 300
