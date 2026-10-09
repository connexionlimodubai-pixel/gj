"""Job boards collector (Greenhouse / Lever / Ashby), fully offline via httpx.MockTransport.

Fixtures in tests/fixtures/jobs mirror the documented public payloads from the API research:
Greenhouse `/v1/boards/{token}/jobs?content=true` ({"jobs": [...], "meta": ...} with entity-escaped
HTML content), Lever `/v0/postings/{site}?mode=json` (a bare array, createdAt in epoch ms) and
Ashby `/posting-api/job-board/{name}` ({"apiVersion", "jobs"} with isListed and publishedAt).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from openberry import repo
from openberry.collectors import COLLECTORS
from openberry.collectors import jobs
from openberry.collectors.base import CollectContext, RawSignal
from openberry.collectors.jobs import JobBoardsCollector
from openberry.config import get_settings
from openberry.models import Company, JobBoard, LeadIn, parse_job_boards
from openberry.services import ingest

FIXTURES = Path(__file__).parent / "fixtures" / "jobs"
SINCE = datetime(2026, 9, 24, tzinfo=timezone.utc)
UTC = timezone.utc

GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
GH = GH_BOARD.format(token="acmebank")
LV = "https://api.lever.co/v0/postings/northwind"
LV_EU = "https://api.eu.lever.co/v0/postings/northwind"
AB = "https://api.ashbyhq.com/posting-api/job-board/lumen"

ROUTES = {GH: "greenhouse_acmebank.json", LV: "lever_northwind.json", AB: "ashby_lumen.json"}
HTML_403 = "<html><head><title>403 Forbidden</title></head><body><h1>403 Forbidden</h1></body></html>"

GH_EA_CEO = "greenhouse:acmebank:7012345002"
GH_SENIOR_EA = "greenhouse:acmebank:7012345003"
GH_TE_MANAGER = "greenhouse:acmebank:7012345004"      # first published before SINCE, updated after
GH_COORDINATOR = "greenhouse:acmebank:7012345005"     # no first_published: falls back to updated_at
LV_TRAVEL_MANAGER = "lever:northwind:5f0c1a2b-0001-4eab-9ee2-aa7d1d07a9d6"
LV_EA = "lever:northwind:5f0c1a2b-0002-4eab-9ee2-aa7d1d07a9d6"   # created before SINCE
LV_UNDATED = "lever:northwind:5f0c1a2b-0003-4eab-9ee2-aa7d1d07a9d6"
AB_EA = "ashby:lumen:7458d4e9-da2e-47bd-98cb-adfda43d42b2"

DEFAULT_IDS = [GH_COORDINATOR, GH_EA_CEO, GH_SENIOR_EA, LV_TRAVEL_MANAGER, LV_UNDATED]

Override = httpx.Response | Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def empty_board(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=[] if "lever.co" in request.url.host else {"jobs": [], "meta": {"total": 0}})


def not_found(request: httpx.Request) -> httpx.Response:
    if "lever.co" in request.url.host:
        return httpx.Response(404, json=fixture("lever_not_found.json"))
    return httpx.Response(404, text="<html><body>Page not found</body></html>",
                          headers={"Content-Type": "text/html"})


class FakeBoards:
    """MockTransport handler: serves fixtures by URL (without query), records requests, injects failures."""

    def __init__(self, overrides: dict[str, Override] | None = None, default: Override = not_found) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides = overrides or {}
        self.default = default

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        override = self.overrides.get(key)
        if override is not None:
            return override(request) if callable(override) else override
        if key in ROUTES:
            return httpx.Response(200, json=fixture(ROUTES[key]))
        return self.default(request) if callable(self.default) else self.default

    @property
    def urls(self) -> list[str]:
        return [f"{r.url.scheme}://{r.url.host}{r.url.path}" for r in self.requests]


async def run(company: Company, handler: FakeBoards, *, since: datetime = SINCE, max_items: int = 200,
              time_budget: float = 60.0, request_timeout: float = 25.0) -> tuple[list[RawSignal], CollectContext]:
    collector = JobBoardsCollector()
    collector.request_interval = 0
    collector.time_budget = time_budget
    collector.request_timeout = request_timeout
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"User-Agent": "OpenBerry-test"}) as client:
        ctx = CollectContext(client=client, since=since, settings=get_settings(), max_items=max_items)
        signals = await collector.collect(company, ctx)
    return signals, ctx


def boards(*specs: str) -> list[JobBoard]:
    return [JobBoard.model_validate(b) for b in parse_job_boards(list(specs))]


def configure(company: Company, *board_specs: str, icp: dict[str, Any] | None = None, **signals: Any) -> Company:
    if board_specs:
        signals["job_boards"] = boards(*board_specs)
    update: dict[str, Any] = {"signals": company.signals.model_copy(update=signals)}
    if icp is not None:
        update["icp"] = company.icp.model_copy(update=icp)
    return company.model_copy(update=update)


def by_id(signals: list[RawSignal]) -> dict[str, RawSignal]:
    return {r.signal.external_id: r for r in signals}


def ids(signals: list[RawSignal]) -> list[str]:
    return [r.signal.external_id for r in signals]


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_registered_with_declared_metadata():
    collector = COLLECTORS["jobs"]
    assert isinstance(collector, JobBoardsCollector)
    assert collector.name == "jobs"
    assert collector.signal_types == ("hiring",)
    assert collector.label == "Job boards (Greenhouse / Lever / Ashby)"
    assert "job_boards" in collector.requires


def test_is_configured(company):
    collector = JobBoardsCollector()
    assert collector.is_configured(company)
    assert collector.enabled_for(company)
    assert not collector.is_configured(configure(company, job_boards=[]))
    # No keywords at all still counts as configured (the boards are the input); collect() warns instead.
    assert collector.is_configured(configure(company, hiring_keywords=[], icp={"job_titles": []}))
    assert not collector.enabled_for(configure(company, enabled_types=["keyword_mention"]))


async def test_sends_expected_requests(company):
    company = configure(company, "greenhouse:acmebank:Acme Bank", "lever:northwind:Northwind", "ashby:lumen:Lumen Labs")
    handler = FakeBoards()
    await run(company, handler)
    assert handler.urls == [GH, LV, AB]
    gh, lv, ab = handler.requests
    assert all(r.method == "GET" for r in handler.requests)
    assert dict(gh.url.params) == {"content": "true"}
    assert dict(lv.url.params) == {"mode": "json"}
    assert str(ab.url) == AB and not ab.url.query
    for request in handler.requests:
        assert request.headers["Accept"] == "application/json"
        assert request.headers["User-Agent"] == "OpenBerry-test"  # the shared client's headers are kept


async def test_hiring_disabled_or_zero_max_items_makes_no_requests(company):
    handler = FakeBoards()
    signals, _ = await run(configure(company, enabled_types=["keyword_mention"]), handler)
    assert signals == [] and handler.requests == []
    signals, _ = await run(company, handler, max_items=0)
    assert signals == [] and handler.requests == []


async def test_no_keywords_warns_without_requests(company):
    handler = FakeBoards()
    signals, ctx = await run(configure(company, hiring_keywords=[], icp={"job_titles": []}), handler)
    assert signals == [] and handler.requests == []
    assert len(ctx.warnings) == 1 and "no hiring keywords or ICP job titles" in ctx.warnings[0]


# --------------------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------------------


async def test_default_scan_returns_new_matching_roles(company):
    signals, ctx = await run(company, FakeBoards())
    assert ctx.warnings == []
    # Per board newest first; undated postings last.
    assert ids(signals) == DEFAULT_IDS
    for raw in signals:
        assert raw.lead is None and raw.account_domain == ""
        assert raw.signal.type == "hiring"
        assert raw.signal.title.startswith("Hiring: ")
        assert raw.signal.occurred_at.utcoffset().total_seconds() == 0
        assert len(raw.signal.summary) <= 500 and "<" not in raw.signal.summary and "&lt;" not in raw.signal.summary


async def test_maps_greenhouse_postings(company):
    signals, _ = await run(company, FakeBoards())
    found = by_id(signals)

    ea = found[GH_EA_CEO]
    assert ea.account == "Acme Bank"
    sig = ea.signal
    assert sig.source == "greenhouse"
    assert sig.title == "Hiring: Executive Assistant to the CEO"
    assert sig.url == "https://job-boards.greenhouse.io/acmebank/jobs/7012345002"
    assert sig.occurred_at == datetime(2026, 10, 1, 13, 15, tzinfo=UTC)  # first_published, -04:00 -> UTC
    assert sig.summary.startswith(
        "Acme Bank is hiring: Executive Assistant to the CEO. "
        "Location: Dubai, United Arab Emirates · Department: Office of the CEO. "
        "Acme Bank is growing its Dubai office.")
    assert "support our CEO & leadership team" in sig.summary          # double-escaped content decoded
    assert sig.strength == 85                                         # 4 matching open roles at Acme Bank
    assert sig.raw == {
        "provider": "greenhouse",
        "board": "acmebank",
        "job_id": "7012345002",
        "job_title": "Executive Assistant to the CEO",
        "matched": ["Executive Assistant"],
        "matched_from": "hiring_keywords",
        "location": "Dubai, United Arab Emirates",
        "department": "Office of the CEO",
        "time_field": "first_published",
        "open_matching_roles": 4,
        "requisition_id": "EA-2026-14",
        "updated_at": "2026-10-03T10:00:00-04:00",
    }

    senior = found[GH_SENIOR_EA].signal
    assert senior.raw["department"] == "Corporate Banking, Wholesale"
    assert senior.occurred_at == datetime(2026, 9, 29, 12, 30, tzinfo=UTC)

    coordinator = found[GH_COORDINATOR]
    assert coordinator.account == "Acme Bank"
    assert coordinator.signal.occurred_at == datetime(2026, 10, 2, 13, 0, tzinfo=UTC)  # updated_at fallback
    assert coordinator.signal.raw["time_field"] == "updated_at"
    assert coordinator.signal.raw["location"] == "Dubai"                               # from offices
    assert "requisition_id" not in coordinator.signal.raw                               # null dropped
    assert "undated" not in coordinator.signal.raw


async def test_maps_lever_postings(company):
    before = datetime.now(UTC).replace(microsecond=0)
    signals, _ = await run(company, FakeBoards())
    after = datetime.now(UTC)
    found = by_id(signals)

    tm = found[LV_TRAVEL_MANAGER]
    assert tm.account == "Northwind" and tm.lead is None
    sig = tm.signal
    assert sig.source == "lever"
    assert sig.title == "Hiring: Travel Manager"
    assert sig.url == "https://jobs.lever.co/northwind/5f0c1a2b-0001-4eab-9ee2-aa7d1d07a9d6"
    assert sig.occurred_at == datetime(2026, 10, 4, 8, 0, tzinfo=UTC)  # createdAt epoch ms
    assert sig.summary.startswith(
        "Northwind is hiring: Travel Manager. Location: Dubai; Riyadh · Department: Operations · "
        "Team: Workplace & Travel · Full-time · On-site. Northwind is looking for a Travel Manager")
    assert sig.strength == 80                                           # 3 matching open roles
    assert sig.raw["matched"] == ["Travel"]
    assert sig.raw["time_field"] == "createdAt"
    assert sig.raw["country"] == "AE"
    assert sig.raw["salary"] == "AED 25,000-32,000 per-month-salary"
    assert sig.raw["apply_url"].endswith("/apply")

    undated = found[LV_UNDATED].signal
    assert undated.raw["undated"] is True
    assert "time_field" not in undated.raw
    assert before <= undated.occurred_at <= after
    assert undated.raw["employment"] == "Contract" and "workplace" not in undated.raw  # "unspecified" dropped
    assert undated.strength == 80


async def test_maps_ashby_postings_and_skips_unlisted(company):
    signals, _ = await run(configure(company, "ashby:lumen:Lumen Labs"), FakeBoards())
    assert ids(signals) == [AB_EA]  # "Head of Travel" is unlisted, "Engineering Manager" does not match
    raw = signals[0]
    assert raw.account == "Lumen Labs" and raw.lead is None
    sig = raw.signal
    assert sig.source == "ashby"
    assert sig.title == "Hiring: Executive Assistant"                  # padded title stripped
    assert sig.url == "https://jobs.ashbyhq.com/lumen/7458d4e9-da2e-47bd-98cb-adfda43d42b2"
    assert sig.occurred_at == datetime(2026, 10, 6, 10, 20, tzinfo=UTC)
    assert sig.summary == (
        "Lumen Labs is hiring: Executive Assistant. Location: Dubai; Remote - UAE · Department: G&A · "
        "Team: Office of the CEO · Full-time · Hybrid. We are looking for an Executive Assistant to keep "
        "our founders on track.")
    assert sig.strength == 60                                          # the unlisted role is not counted
    assert sig.raw["open_matching_roles"] == 1
    assert sig.raw["time_field"] == "publishedAt"
    assert sig.raw["apply_url"].endswith("/application")


async def test_board_without_company_name_uses_greenhouse_company_name_or_token(company):
    handler = FakeBoards(overrides={
        GH_BOARD.format(token="acme2"): httpx.Response(200, json=fixture("greenhouse_acmebank.json")),
    })
    company = configure(company, job_boards=[
        JobBoard(provider="greenhouse", token="acme2"),
        JobBoard(provider="lever", token="northwind"),
    ])
    signals, _ = await run(company, handler)
    accounts = {r.signal.source: r.account for r in signals}
    assert accounts == {"greenhouse": "Acme Bank", "lever": "northwind"}


# --------------------------------------------------------------------------------------
# Matching, time window, strength
# --------------------------------------------------------------------------------------


async def test_only_postings_newer_than_since(company):
    signals, _ = await run(company, FakeBoards())
    # Old roles are skipped even when their updated_at is recent (Greenhouse) ...
    assert GH_TE_MANAGER not in ids(signals) and LV_EA not in ids(signals)

    later = datetime(2026, 10, 2, tzinfo=UTC)
    signals, _ = await run(company, FakeBoards(), since=later)
    assert ids(signals) == [GH_COORDINATOR, LV_TRAVEL_MANAGER, LV_UNDATED]  # undated is always kept
    assert by_id(signals)[GH_COORDINATOR].signal.strength == 85  # all open matching roles still count

    earlier = datetime(2026, 7, 1, tzinfo=UTC)
    signals, _ = await run(company, FakeBoards(), since=earlier)
    assert {GH_TE_MANAGER, LV_EA} <= set(ids(signals))
    assert len(signals) == 7


async def test_naive_since_is_treated_as_utc(company):
    signals, _ = await run(company, FakeBoards(), since=datetime(2026, 10, 2))
    assert ids(signals) == [GH_COORDINATOR, LV_TRAVEL_MANAGER, LV_UNDATED]


async def test_falls_back_to_icp_job_titles(company):
    company = configure(company, hiring_keywords=[])  # ICP: "Executive Assistant", "Travel Manager"
    signals, _ = await run(company, FakeBoards())
    assert ids(signals) == [GH_EA_CEO, GH_SENIOR_EA, LV_TRAVEL_MANAGER]
    found = by_id(signals)
    assert all(r.signal.raw["matched_from"] == "icp.job_titles" for r in signals)
    assert found[GH_EA_CEO].signal.strength == 80       # EA x2 + "Travel & Expense Manager" (old) = 3
    assert found[LV_TRAVEL_MANAGER].signal.strength == 70  # Travel Manager + Executive Assistant (old) = 2
    assert found[LV_TRAVEL_MANAGER].signal.raw["matched"] == ["Travel Manager"]


def test_title_matching_is_word_based_and_order_insensitive():
    ctx = CollectContext(client=None, since=SINCE, settings=get_settings())  # type: ignore[arg-type]
    items = [
        {"id": 1, "title": "Manager, Travel & Events", "first_published": "2026-10-01T00:00:00Z"},
        {"id": 2, "title": "Field Auditor (Travelling)", "first_published": "2026-10-01T00:00:00Z"},
        {"id": 3, "title": "SENIOR EXECUTIVE ASSISTANT", "first_published": "2026-10-02T00:00:00Z"},
        {"id": 4, "title": "Assistant Manager", "first_published": "2026-10-03T00:00:00Z"},
        {"id": 3, "title": "SENIOR EXECUTIVE ASSISTANT", "first_published": "2026-10-02T00:00:00Z"},  # dup
    ]
    out = jobs.matching_postings(jobs.PROVIDERS["greenhouse"], "acme", items,
                                 ["Travel Manager", "Executive Assistant"], ctx)
    assert [(p.job_id, matched) for p, matched in out] == [
        ("3", ["Executive Assistant"]), ("1", ["Travel Manager"])]
    assert ctx.warnings == []


@pytest.mark.parametrize("roles, strength", [(0, 60), (1, 60), (2, 70), (3, 80), (4, 85), (12, 85)])
def test_role_strength_rule(roles, strength):
    assert jobs.role_strength(roles) == strength


async def test_roles_on_two_boards_of_one_account_are_counted_together(company):
    company = configure(company, "lever:northwind:Northwind", "ashby:lumen:Northwind")
    signals, _ = await run(company, FakeBoards())
    assert {r.account for r in signals} == {"Northwind"}
    assert {r.signal.strength for r in signals} == {85}   # 3 on Lever + 1 on Ashby
    assert {r.signal.raw["open_matching_roles"] for r in signals} == {4}


# --------------------------------------------------------------------------------------
# Stable ids, dedupe, caps
# --------------------------------------------------------------------------------------


async def test_ids_are_stable_and_duplicates_dropped(company):
    first, _ = await run(company, FakeBoards())
    second, _ = await run(company, FakeBoards())
    assert ids(first) == ids(second) == DEFAULT_IDS
    assert [r.signal.occurred_at for r in first[:4]] == [r.signal.occurred_at for r in second[:4]]

    # The same board twice (token case differs) is fetched once; a job listed twice yields one signal.
    payload = fixture("greenhouse_acmebank.json")
    payload["jobs"].append(dict(payload["jobs"][0]))
    handler = FakeBoards(overrides={GH: httpx.Response(200, json=payload),
                                    GH_BOARD.format(token="AcmeBank"): httpx.Response(500)})
    company = configure(company, "greenhouse:acmebank:Acme Bank", "greenhouse:AcmeBank:Acme Bank")
    signals, ctx = await run(company, handler)
    assert handler.urls == [GH]
    assert ids(signals) == [GH_COORDINATOR, GH_EA_CEO, GH_SENIOR_EA]
    assert signals[0].signal.raw["open_matching_roles"] == 4
    assert ctx.warnings == []


async def test_external_id_uses_lowercase_token(company):
    handler = FakeBoards(overrides={
        GH_BOARD.format(token="AcmeBank"): httpx.Response(200, json=fixture("greenhouse_acmebank.json")),
    })
    signals, _ = await run(configure(company, "greenhouse:AcmeBank:Acme Bank"), handler)
    assert ids(signals) == [GH_COORDINATOR, GH_EA_CEO, GH_SENIOR_EA]
    assert signals[0].signal.raw["board"] == "AcmeBank"


async def test_board_cap_and_daily_rotation(company):
    specs = [f"greenhouse:board{i:02d}:Board {i}" for i in range(35)]
    handler = FakeBoards(default=empty_board)
    signals, ctx = await run(configure(company, *specs), handler)
    assert signals == []
    assert len(handler.requests) == jobs.MAX_BOARDS_PER_SCAN == 30
    start = (SINCE.date().toordinal() * 30) % 35   # the window moves by a whole scan's worth per day
    expected = [(start + i) % 35 for i in range(30)]
    assert handler.urls == [GH_BOARD.format(token=f"board{i:02d}") for i in expected]
    assert ctx.warnings == ["Job boards: 35 boards configured but only 30 are checked per scan; "
                            "the checked set rotates daily"]


@pytest.mark.parametrize("n_boards, max_gap_days", [(31, 2), (60, 2), (100, 4)])
def test_rotation_checks_every_board_well_inside_the_lookback(n_boards, max_gap_days):
    # Regression: moving the window by one board a day left boards unchecked for up to n - 29 days
    # (31 days with 60 boards), longer than the 14-day lookback, so their new postings were never seen.
    configured = boards(*[f"lever:site{i:03d}:Site {i}" for i in range(n_boards)])
    last_seen: dict[str, int] = {}
    worst = 0
    for day in range(60):
        ctx = CollectContext(client=None, since=SINCE + timedelta(days=day), settings=get_settings())  # type: ignore[arg-type]
        for board in jobs.select_boards(configured, ctx):
            if board.token in last_seen:
                worst = max(worst, day - last_seen[board.token])
            last_seen[board.token] = day
    assert len(last_seen) == n_boards
    assert worst <= max_gap_days


async def test_request_cap_counts_lever_eu_retries(company):
    specs = [f"lever:site{i:02d}:Site {i}" for i in range(20)]
    handler = FakeBoards()  # every site is unknown on both hosts
    signals, ctx = await run(configure(company, *specs), handler)
    assert signals == []
    assert len(handler.requests) == jobs.MAX_REQUESTS_PER_SCAN == 34
    assert handler.urls[:2] == ["https://api.lever.co/v0/postings/site00", "https://api.eu.lever.co/v0/postings/site00"]
    not_found = [w for w in ctx.warnings if w.endswith("not found")]
    assert len(not_found) == 17 and not_found[0] == "lever board 'site00' not found"
    assert ctx.warnings[-1] == "Job boards: request cap reached (34 per scan); 3 board(s) not checked this scan"


async def test_max_items_stops_fetching_more_boards(company):
    handler = FakeBoards()
    signals, ctx = await run(company, handler, max_items=2)
    assert ids(signals) == [GH_COORDINATOR, GH_EA_CEO]
    assert handler.urls == [GH]
    assert ctx.warnings == ["Job boards: reached the limit of 2 signals; "
                            "1 more matching role(s) dropped and 1 board(s) not checked this scan"]

    # Exactly at the limit with nothing left to check: no warning.
    signals, ctx = await run(configure(company, "greenhouse:acmebank:Acme Bank"), FakeBoards(), max_items=3)
    assert len(signals) == 3 and ctx.warnings == []
    # The last board overflows: the oldest roles are dropped.
    signals, ctx = await run(configure(company, "greenhouse:acmebank:Acme Bank"), FakeBoards(), max_items=1)
    assert ids(signals) == [GH_COORDINATOR]
    assert ctx.warnings == ["Job boards: reached the limit of 1 signals; 2 more matching role(s) dropped this scan"]


async def test_time_budget_stops_before_requests(company):
    handler = FakeBoards()
    signals, ctx = await run(company, handler, time_budget=0)
    assert signals == [] and handler.requests == []
    assert ctx.warnings == ["Job boards: time budget for this scan used up; 2 board(s) not checked this scan"]


# --------------------------------------------------------------------------------------
# Errors: warnings, never exceptions
# --------------------------------------------------------------------------------------


async def test_unknown_board_warns_and_continues(company):
    company = configure(company, "greenhouse:nosuchbank:No Such Bank", "lever:northwind:Northwind", "ashby:ghost:Ghost")
    handler = FakeBoards()
    signals, ctx = await run(company, handler)
    assert ids(signals) == [LV_TRAVEL_MANAGER, LV_UNDATED]
    assert ctx.warnings == ["greenhouse board 'nosuchbank' not found", "ashby board 'ghost' not found"]


async def test_lever_falls_back_to_the_eu_host(company):
    handler = FakeBoards(overrides={
        LV: httpx.Response(404, json=fixture("lever_not_found.json")),
        LV_EU: httpx.Response(200, json=fixture("lever_northwind.json")),
    })
    signals, ctx = await run(configure(company, "lever:northwind:Northwind"), handler)
    assert handler.urls == [LV, LV_EU]
    assert all(dict(r.url.params) == {"mode": "json"} for r in handler.requests)
    assert ids(signals) == [LV_TRAVEL_MANAGER, LV_UNDATED]
    assert ctx.warnings == []


async def test_lever_ok_false_body_counts_as_not_found(company):
    handler = FakeBoards(overrides={LV: httpx.Response(200, json=fixture("lever_not_found.json")),
                                    LV_EU: httpx.Response(200, json=fixture("lever_not_found.json"))})
    signals, ctx = await run(configure(company, "lever:northwind:Northwind"), handler)
    assert signals == [] and handler.urls == [LV, LV_EU]
    assert ctx.warnings == ["lever board 'northwind' not found"]


async def test_403_html_skips_the_provider_but_not_the_others(company):
    company = configure(company, "greenhouse:acmebank:Acme Bank", "greenhouse:otherbank:Other Bank",
                        "lever:northwind:Northwind")
    handler = FakeBoards(overrides={GH: httpx.Response(403, text=HTML_403, headers={"Content-Type": "text/html"})})
    signals, ctx = await run(company, handler)
    assert handler.urls == [GH, LV]
    assert ids(signals) == [LV_TRAVEL_MANAGER, LV_UNDATED]
    assert ctx.warnings == ["greenhouse refused access (HTTP 403) to board 'acmebank'; "
                            "skipped its remaining boards this scan"]


async def test_429_skips_the_provider_and_reports_retry_after(company):
    company = configure(company, "greenhouse:acmebank:Acme Bank", "greenhouse:otherbank:Other Bank",
                        "lever:northwind:Northwind")
    handler = FakeBoards(overrides={GH: httpx.Response(429, headers={"Retry-After": "120"}, text="Too Many Requests")})
    signals, ctx = await run(company, handler)
    assert handler.urls == [GH, LV]
    assert ids(signals) == [LV_TRAVEL_MANAGER, LV_UNDATED]
    assert ctx.warnings == ["greenhouse: rate limited (HTTP 429, retry after 120); "
                            "skipped its remaining boards this scan"]


async def test_rate_limit_header_stops_the_provider_after_this_response(company):
    company = configure(company, "greenhouse:acmebank:Acme Bank", "greenhouse:otherbank:Other Bank")
    handler = FakeBoards(overrides={
        GH: httpx.Response(200, json=fixture("greenhouse_acmebank.json"), headers={"X-RateLimit-Remaining": "0"}),
    })
    signals, ctx = await run(company, handler)
    assert handler.urls == [GH]
    assert ids(signals) == [GH_COORDINATOR, GH_EA_CEO, GH_SENIOR_EA]
    assert ctx.warnings == ["greenhouse: rate-limit budget exhausted; skipped its remaining boards this scan"]


def _raise(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


async def test_500_malformed_json_and_timeouts_warn(company):
    company = configure(company, "greenhouse:acmebank:Acme Bank", "lever:northwind:Northwind", "ashby:lumen:Lumen")
    handler = FakeBoards(overrides={
        GH: httpx.Response(500, text="Internal Server Error"),
        LV: httpx.Response(200, text="{not json", headers={"Content-Type": "application/json"}),
        AB: _raise(httpx.ReadTimeout("timed out")),
    })
    signals, ctx = await run(company, handler)
    assert signals == []
    assert handler.urls == [GH, LV, AB]
    assert ctx.warnings == [
        "greenhouse board 'acmebank': HTTP 500",
        "lever board 'northwind': response was not valid JSON",
        "ashby board 'lumen': request timed out",
    ]


async def test_slow_response_is_cut_off_by_the_request_timeout(company):
    # httpx timeouts are per read, so a board trickling in slowly was unbounded and could push the
    # collector past services.COLLECTOR_TIMEOUT_SECONDS, which throws away every signal found so far.
    async def trickle(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=fixture("greenhouse_acmebank.json"))

    handler = FakeBoards(overrides={GH: trickle})
    started = time.monotonic()
    signals, ctx = await run(company, handler, request_timeout=0.05)
    assert time.monotonic() - started < 2
    assert handler.urls == [GH, LV]
    assert ids(signals) == [LV_TRAVEL_MANAGER, LV_UNDATED]
    assert ctx.warnings == ["greenhouse board 'acmebank': request timed out"]


def test_time_budget_plus_request_timeout_fits_the_scan_timeout():
    from openberry.services import COLLECTOR_TIMEOUT_SECONDS

    collector = JobBoardsCollector()
    assert collector.time_budget + collector.request_timeout < COLLECTOR_TIMEOUT_SECONDS


async def test_connection_errors_never_raise(company):
    handler = FakeBoards(overrides={GH: _raise(httpx.ConnectError("no route to host")),
                                    LV: _raise(httpx.RemoteProtocolError("peer closed connection"))})
    signals, ctx = await run(company, handler)
    assert signals == []
    assert ctx.warnings == ["greenhouse board 'acmebank': request failed (ConnectError)",
                            "lever board 'northwind': request failed (RemoteProtocolError)"]


async def test_three_failures_in_a_row_skip_the_provider(company):
    specs = [f"greenhouse:bank{i}:Bank {i}" for i in range(5)] + ["lever:northwind:Northwind"]
    handler = FakeBoards(overrides={
        GH_BOARD.format(token=f"bank{i}"): httpx.Response(502) for i in range(5)})
    signals, ctx = await run(configure(company, *specs), handler)
    assert handler.urls[:3] == [GH_BOARD.format(token=f"bank{i}") for i in range(3)]
    assert handler.urls[3:] == [LV]
    assert ids(signals) == [LV_TRAVEL_MANAGER, LV_UNDATED]
    assert ctx.warnings[-1] == "greenhouse: 3 failed requests in a row; skipped its remaining boards this scan"


async def test_failure_then_success_resets_the_failure_count(company):
    specs = ["greenhouse:bank0:Bank 0", "greenhouse:bank1:Bank 1", "greenhouse:acmebank:Acme Bank",
             "greenhouse:bank3:Bank 3", "greenhouse:bank4:Bank 4"]
    handler = FakeBoards(overrides={
        GH_BOARD.format(token=f"bank{i}"): httpx.Response(503) for i in (0, 1, 3, 4)})
    signals, ctx = await run(configure(company, *specs), handler)
    assert len(handler.requests) == 5
    assert len(signals) == 3
    assert all("in a row" not in w for w in ctx.warnings)


@pytest.mark.parametrize("payload", [[], {"error": "board disabled"}, {"jobs": "nope"}, "jobs"])
async def test_unexpected_payload_shape_warns(company, payload):
    handler = FakeBoards(overrides={GH: httpx.Response(200, json=payload)})
    signals, ctx = await run(configure(company, "greenhouse:acmebank:Acme Bank"), handler)
    assert signals == []
    assert ctx.warnings == ["greenhouse board 'acmebank': unexpected response format"]


async def test_malformed_postings_are_skipped_not_fatal(company):
    good = fixture("greenhouse_acmebank.json")["jobs"][0]
    payload = {"jobs": [
        "not a posting",
        {**good, "id": None},
        {**good, "id": 1, "title": {"text": "Executive Assistant"}},
        {**good, "id": 2, "title": "   "},
        {**good, "id": 3, "first_published": "not a date", "updated_at": "2026-10-07T09:00:00Z"},
        {**good, "id": 4, "first_published": True, "updated_at": 10**20},   # unusable times -> undated
        {**good, "id": 5, "location": "Dubai", "departments": "Office", "offices": None,
         "absolute_url": "javascript:alert(1)", "content": None},
    ], "meta": {"total": 7}}
    handler = FakeBoards(overrides={GH: httpx.Response(200, json=payload)})
    signals, ctx = await run(configure(company, "greenhouse:acmebank:Acme Bank"), handler)
    found = by_id(signals)
    assert set(found) == {"greenhouse:acmebank:3", "greenhouse:acmebank:4", "greenhouse:acmebank:5"}
    assert found["greenhouse:acmebank:3"].signal.occurred_at == datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
    assert found["greenhouse:acmebank:4"].signal.raw["undated"] is True
    weird = found["greenhouse:acmebank:5"].signal
    assert weird.url == "https://job-boards.greenhouse.io/acmebank/jobs/5"
    assert weird.summary == "Acme Bank is hiring: Executive Assistant to the CEO."
    assert ctx.warnings == ["greenhouse board 'acmebank': skipped 4 malformed posting(s)"]


async def test_dot_only_tokens_are_skipped_without_a_request(company):
    # "." and ".." pass JobBoard's token check, but httpx collapses them into another path
    # (https://api.lever.co/v0?mode=json), so they must never be requested.
    company = configure(company, job_boards=[JobBoard(provider="lever", token=".."),
                                             JobBoard(provider="ashby", token="."),
                                             JobBoard(provider="lever", token="northwind", company="Northwind")])
    handler = FakeBoards()
    signals, ctx = await run(company, handler)
    assert handler.urls == [LV]
    assert ids(signals) == [LV_TRAVEL_MANAGER, LV_UNDATED]
    assert ctx.warnings == ["lever board '..': not a valid board token; skipped",
                            "ashby board '.': not a valid board token; skipped"]


async def test_textarea_board_without_company_uses_the_greenhouse_company_name(company):
    # parse_job_boards fills a missing company with the token, so "greenhouse:acmebank" used to give
    # the account "acmebank" even though every Greenhouse posting carries company_name "Acme Bank".
    signals, _ = await run(configure(company, "greenhouse:acmebank", "lever:northwind"), FakeBoards())
    accounts = {r.signal.source: r.account for r in signals}
    assert accounts == {"greenhouse": "Acme Bank", "lever": "northwind"}
    assert by_id(signals)[GH_EA_CEO].signal.summary.startswith("Acme Bank is hiring:")
    # A company name the user typed always wins over the payload's.
    signals, _ = await run(configure(company, "greenhouse:acmebank:ACME Bank PJSC"), FakeBoards())
    assert {r.account for r in signals} == {"ACME Bank PJSC"}


async def test_roles_are_counted_per_account_identity_not_spelling(company):
    # "Northwind" and "Northwind Ltd" are one account lead (repo.company_key), so their roles add up.
    company = configure(company, "lever:northwind:Northwind", "ashby:lumen:Northwind Ltd.")
    signals, _ = await run(company, FakeBoards())
    assert {r.signal.raw["open_matching_roles"] for r in signals} == {4}
    assert {r.signal.strength for r in signals} == {85}
    stats = ingest(company.id, signals)
    assert stats.errors == [] and repo.list_leads(company.id, kind="account")[1] == 1


async def test_documented_example_payloads_map_field_by_field(company):
    # The verbatim example responses from the API research (not this test suite's own fixtures),
    # so a field-name mistake in the collector cannot hide behind a fixture with the same mistake.
    handler = FakeBoards(overrides={
        GH_BOARD.format(token="discord"): httpx.Response(200, json=fixture("documented_greenhouse.json")),
        "https://api.lever.co/v0/postings/spotify": httpx.Response(200, json=fixture("documented_lever.json")),
        "https://api.ashbyhq.com/posting-api/job-board/ashby": httpx.Response(200, json=fixture("documented_ashby.json")),
    })
    company = configure(company, "greenhouse:discord", "lever:spotify:Spotify", "ashby:ashby:Ashby",
                        hiring_keywords=["Software Engineer", "Account Executive", "Engineering Manager"])
    signals, ctx = await run(company, handler, since=datetime(2024, 1, 1, tzinfo=UTC))
    assert ctx.warnings == []
    found = {r.signal.source: r for r in signals}
    assert set(found) == {"greenhouse", "lever", "ashby"}

    gh = found["greenhouse"]
    assert gh.account == "Discord"
    assert gh.signal.external_id == "greenhouse:discord:8642213002"
    assert gh.signal.title == "Hiring: Software Engineer, Notifications"
    assert gh.signal.url == "https://job-boards.greenhouse.io/discord/jobs/8642213002"
    assert gh.signal.occurred_at == datetime(2026, 9, 11, 17, 13, 31, tzinfo=UTC)
    assert gh.signal.raw["time_field"] == "first_published"
    assert gh.signal.raw["location"] == "San Francisco Bay Area"
    assert gh.signal.raw["department"] == "Product Engineering"
    assert gh.signal.raw["requisition_id"] == "R-107350"
    assert "Discord has a highly engaged community" in gh.signal.summary and "&lt;" not in gh.signal.summary

    lv = found["lever"]
    assert lv.signal.external_id == "lever:spotify:1ff4a4e3-897c-4eab-9ee2-aa7d1d07a9d6"
    assert lv.signal.title == "Hiring: Account Executive, Backstage"
    assert lv.signal.url == "https://jobs.lever.co/spotify/1ff4a4e3-897c-4eab-9ee2-aa7d1d07a9d6"
    assert lv.signal.occurred_at == datetime(2026, 3, 29, 10, 0, tzinfo=UTC)
    assert lv.signal.raw["time_field"] == "createdAt"
    assert (lv.signal.raw["location"], lv.signal.raw["department"], lv.signal.raw["team"]) == (
        "Toronto", "Operations and Business Support", "Platform")
    assert lv.signal.raw["employment"] == "Permanent" and lv.signal.raw["country"] == "CA"
    assert "workplace" not in lv.signal.raw                      # "unspecified"
    assert "As an Account Executive for Spotify Backstage" in lv.signal.summary

    ab = found["ashby"]
    assert ab.signal.external_id == "ashby:ashby:7458d4e9-da2e-47bd-98cb-adfda43d42b2"
    assert ab.signal.title == "Hiring: Engineering Manager, EU"
    assert ab.signal.url == "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2"
    assert ab.signal.occurred_at == datetime(2024, 3, 4, 14, 29, 8, 532000, tzinfo=UTC)
    assert ab.signal.raw["time_field"] == "publishedAt"
    assert ab.signal.raw["location"] == "Remote - European Union; Spain"
    assert (ab.signal.raw["department"], ab.signal.raw["team"]) == ("Engineering", "EMEA Engineering")
    assert (ab.signal.raw["employment"], ab.signal.raw["workplace"]) == ("Full-time", "Remote")
    assert ab.signal.raw["apply_url"].endswith("/application")


async def test_ashby_posting_without_id_uses_the_url_slug(company):
    payload = fixture("ashby_lumen.json")
    del payload["jobs"][0]["id"]
    handler = FakeBoards(overrides={AB: httpx.Response(200, json=payload)})
    signals, _ = await run(configure(company, "ashby:lumen:Lumen Labs"), handler)
    assert ids(signals) == [AB_EA]


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------


async def test_ingest_creates_scored_account_leads(company):
    jane, _ = repo.upsert_lead(company.id, LeadIn(full_name="Jane Doe", title="Travel Manager",
                                                  lead_company="Acme Bank", location="Dubai"))
    assert jane.intent_score == 0

    signals, _ = await run(company, FakeBoards())
    stats = ingest(company.id, signals)
    assert stats.errors == []
    assert stats.signals_new == len(signals) == 5
    assert stats.leads_new == 2

    accounts, total = repo.list_leads(company.id, kind="account")
    assert total == 2
    by_name = {lead.lead_company: lead for lead in accounts}
    assert set(by_name) == {"Acme Bank", "Northwind"}
    acme = by_name["Acme Bank"]
    assert acme.source == "greenhouse" and acme.full_name == "" and acme.company_domain == ""
    assert acme.intent_score > 0 and acme.score > 0 and acme.last_signal_at is not None
    assert any("Hiring for a relevant role" in r for r in acme.score_reasons)
    assert by_name["Northwind"].source == "lever"

    acme_signals, n = repo.list_signals(company.id, lead_id=acme.id)
    assert n == 3 and {s.external_id for s in acme_signals} == {GH_EA_CEO, GH_SENIOR_EA, GH_COORDINATOR}
    assert {s.strength for s in acme_signals} == {85}
    lever_signals, _ = repo.list_signals(company.id, source="lever")
    assert {s.external_id for s in lever_signals} == {LV_TRAVEL_MANAGER, LV_UNDATED}

    # A person at the account inherits the hiring intent.
    jane = repo.get_lead(jane.id)
    assert jane.intent_score > 0
    assert any("(company)" in r for r in jane.score_reasons)

    # The next scan finds the same postings: nothing new, same accounts.
    again = ingest(company.id, (await run(company, FakeBoards()))[0])
    assert again.signals_new == 0 and again.signals_duplicate == 5 and again.leads_new == 0
    assert repo.list_leads(company.id, kind="account")[1] == 2


async def test_run_scan_uses_the_collector(company):
    from openberry.services import run_scan

    handler = FakeBoards()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stats = await run_scan(company.id, sources=["jobs"], client=client)
    assert stats["status"] == "ok"
    assert handler.urls == [GH, LV]
    jobs_stats = stats["collectors"]["jobs"]
    assert jobs_stats["warnings"] == []
    # run_scan looks back from the real clock: the undated Lever posting is always in, dated ones while recent.
    assert jobs_stats["found"] >= 1 and stats["signals_new"] == jobs_stats["found"]
    accounts, _ = repo.list_leads(company.id, kind="account")
    assert "Northwind" in {lead.lead_company for lead in accounts}
