"""SEC EDGAR collector (EFTS full-text search: Form D + 8-K Item 5.02), fully offline via httpx.MockTransport.

Fixtures in tests/fixtures/sec_edgar mirror the EFTS search-index payloads documented in the API
research (Elasticsearch-style {"hits": {"total": ..., "hits": [{"_id", "_source": {...}}]}} with
display_names like "Name  (CIK 0001234567)" / "Name (TICK, TICKW) (CIK ...)", per-document hits that
repeat an adsh, Form D exemption codes in `items` and 8-K item numbers in `items`) and SEC's 403 page
for undeclared automated tools.
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
from openberry.collectors import COLLECTORS, sec_edgar
from openberry.collectors.base import CollectContext, RawSignal
from openberry.collectors.sec_edgar import SecEdgarCollector, is_pooled_fund, split_display_name
from openberry.config import get_settings
from openberry.models import Company, LeadIn
from openberry.services import ingest

FIXTURES = Path(__file__).parent / "fixtures" / "sec_edgar"
UTC = timezone.utc
TODAY = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 24, 9, 30, tzinfo=UTC)
EFTS = "https://efts.sec.gov/LATEST/search-index"
EMAIL = "ops@acme.example"
QUERY = "logistics software"
Q = '"logistics software"'

D_FREIGHTWISE = "sec_edgar:D:0002091234-26-000001"
D_PALLETLY = "sec_edgar:D:0002093333-26-000001"      # filed on SINCE's day (kept: day precision)
D_HAULMATIC = "sec_edgar:D:0002093456-26-000002"
K_ROUTELOGIC = "sec_edgar:8-K:0001193125-26-212345"  # main document + EX-99.1 hit, one signal
K_FREIGHTWISE = "sec_edgar:8-K:0000923456-26-000017"
ALL_IDS = [D_FREIGHTWISE, D_PALLETLY, D_HAULMATIC, K_ROUTELOGIC, K_FREIGHTWISE]

FORM_D_FIXTURE = "formd_logistics_software.json"
EIGHT_K_FIXTURE = "8k_logistics_software.json"
EMPTY = {"took": 3, "timed_out": False, "hits": {"total": {"value": 0, "relation": "eq"}, "max_score": None, "hits": []}}

Reply = httpx.Response | Callable[[httpx.Request], httpx.Response] | str | dict


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def default_routes() -> dict[tuple[str, str, int], Reply]:
    return {(Q, "D", 0): FORM_D_FIXTURE, (Q, "8-K", 0): EIGHT_K_FIXTURE}


class FakeEdgar:
    """MockTransport handler: answers by (q, forms, from), records requests, injects failures."""

    def __init__(self, overrides: dict[tuple[str, str, int], Reply] | None = None,
                 default: Reply | None = None, routes: dict[tuple[str, str, int], Reply] | None = None) -> None:
        self.routes = default_routes() if routes is None else routes
        self.routes.update(overrides or {})
        self.default = EMPTY if default is None else default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.params
        reply = self.routes.get((p.get("q", ""), p.get("forms", ""), int(p.get("from", "0"))), self.default)
        if callable(reply):
            return reply(request)
        if isinstance(reply, str):
            return httpx.Response(200, json=fixture(reply))
        if isinstance(reply, dict):
            return httpx.Response(200, json=reply)
        return reply

    @property
    def searches(self) -> list[tuple[str, str, str | None]]:
        return [(r.url.params["q"], r.url.params["forms"], r.url.params.get("from")) for r in self.requests]


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sec_edgar, "_utcnow", lambda: TODAY)


async def run(company: Company, handler: FakeEdgar, *, since: datetime = SINCE, max_items: int = 200,
              time_budget: float = 60.0) -> tuple[list[RawSignal], CollectContext]:
    collector = SecEdgarCollector()
    collector.request_interval = 0
    collector.time_budget = time_budget
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"User-Agent": "OpenBerry-test"}) as client:
        ctx = CollectContext(client=client, since=since, settings=get_settings(), max_items=max_items)
        signals = await collector.collect(company, ctx)
    return signals, ctx


def configure(company: Company, *, contact_email: str = EMAIL, **signals: Any) -> Company:
    signals.setdefault("sec_queries", [QUERY])
    return company.model_copy(update={"contact_email": contact_email,
                                      "signals": company.signals.model_copy(update=signals)})


def ids(signals: list[RawSignal]) -> list[str]:
    return [r.signal.external_id for r in signals]


def by_id(signals: list[RawSignal]) -> dict[str, RawSignal]:
    return {r.signal.external_id: r for r in signals}


def make_hit(n: int, *, form: str = "D", file_date: str = "2026-10-01", items: list[str] | None = None,
             name: str | None = None) -> dict[str, Any]:
    """A synthetic operating-company hit with a unique adsh."""
    cik = f"{2_100_000 + n:010d}"
    adsh = f"{cik}-26-{n:06d}"
    return {
        "_index": "edgar_file",
        "_id": f"{adsh}:primary_doc.xml",
        "_score": 10.0,
        "_source": {
            "ciks": [cik], "display_names": [f"{name or f'Synthetic Freight {n} Inc.'}  (CIK {cik})"],
            "root_forms": [form], "form": form, "file_type": form, "adsh": adsh, "file_date": file_date,
            "biz_locations": ["Denver, CO"], "inc_states": ["DE"],
            "items": items if items is not None else (["06B"] if form == "D" else ["5.02"]),
        },
    }


def page(hits: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"took": 5, "timed_out": False,
            "hits": {"total": {"value": len(hits) if total is None else total, "relation": "eq"},
                     "max_score": 10.0, "hits": hits}}


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_registered_with_declared_metadata():
    collector = COLLECTORS["sec_edgar"]
    assert isinstance(collector, SecEdgarCollector)
    assert collector.name == "sec_edgar"
    assert collector.label == "SEC EDGAR (US funding & exec changes)"
    assert collector.signal_types == ("funding", "job_change")
    assert collector.requires.startswith("sec_queries")
    assert SecEdgarCollector.request_interval >= 0.1   # polite by default: SEC allows 10 req/s


def test_is_configured(company):
    collector = SecEdgarCollector()
    assert not collector.is_configured(company)         # the fixture company has no SEC queries
    assert not collector.enabled_for(company)
    configured = configure(company)
    assert collector.is_configured(configured)
    assert collector.enabled_for(configured)
    # The contact e-mail is not part of is_configured: collect() explains what is missing instead.
    assert collector.is_configured(configure(company, contact_email=""))
    assert not collector.enabled_for(configure(company, enabled_types=["keyword_mention", "hiring"]))
    assert collector.enabled_for(configure(company, enabled_types=["job_change"]))


# --------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------


async def test_sends_declared_user_agent_and_documented_params(company):
    handler = FakeEdgar()
    await run(configure(company), handler)
    assert len(handler.requests) == 2
    for request, form in zip(handler.requests, ("D", "8-K")):
        assert request.method == "GET"
        assert f"{request.url.scheme}://{request.url.host}{request.url.path}" == EFTS
        assert dict(request.url.params) == {"q": Q, "forms": form, "dateRange": "custom",
                                            "startdt": "2026-09-24", "enddt": "2026-10-08"}
        assert request.headers["User-Agent"] == f"OpenBerry {EMAIL}"
        assert request.headers["Accept"] == "application/json"


async def test_settings_contact_email_wins_over_company_email(company, settings):
    settings.contact_email = "admin@openberry.example"
    handler = FakeEdgar()
    await run(configure(company, contact_email="sales@acme.example"), handler)
    assert {r.headers["User-Agent"] for r in handler.requests} == {"OpenBerry admin@openberry.example"}


async def test_invalid_settings_email_falls_back_to_company_email(company, settings):
    settings.contact_email = "not-an-email"
    handler = FakeEdgar()
    await run(configure(company, contact_email="sales@acme.example"), handler)
    assert {r.headers["User-Agent"] for r in handler.requests} == {"OpenBerry sales@acme.example"}


async def test_without_contact_email_nothing_is_fetched(company):
    handler = FakeEdgar()
    signals, ctx = await run(configure(company, contact_email=""), handler)
    assert signals == []
    assert handler.requests == []
    assert ctx.warnings == [
        "SEC EDGAR needs a contact e-mail: set OPENBERRY_CONTACT_EMAIL or the company contact e-mail"]


async def test_quoted_queries_are_sent_unchanged(company):
    boolean = '"freight audit" OR "fleet telematics"'
    handler = FakeEdgar()
    await run(configure(company, sec_queries=[boolean, "  cold chain  "]), handler)
    assert handler.searches == [(boolean, "D", None), (boolean, "8-K", None),
                                ('"cold chain"', "D", None), ('"cold chain"', "8-K", None)]


async def test_only_enabled_signal_types_are_searched(company):
    handler = FakeEdgar()
    signals, _ = await run(configure(company, enabled_types=["funding"]), handler)
    assert handler.searches == [(Q, "D", None)]
    assert {r.signal.type for r in signals} == {"funding"}

    handler = FakeEdgar()
    signals, _ = await run(configure(company, enabled_types=["job_change"]), handler)
    assert handler.searches == [(Q, "8-K", None)]
    assert ids(signals) == [K_ROUTELOGIC, K_FREIGHTWISE]


async def test_requests_are_paced(company, monkeypatch):
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(sec_edgar, "_sleep", record)
    collector = SecEdgarCollector()
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeEdgar())) as client:
        ctx = CollectContext(client=client, since=SINCE, settings=get_settings())
        await collector.collect(configure(company), ctx)
    assert sleeps == [SecEdgarCollector.request_interval]   # two sequential requests, one pause between


# --------------------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------------------


async def test_collects_new_form_d_rounds_and_executive_changes(company):
    signals, ctx = await run(configure(company), FakeEdgar())
    # Skipped: the 3(c)(1) fund, the D/A amendment, the "Fund II" and "SPV ... a Series of" vehicles,
    # the filing from before SINCE's day, the duplicate document hit, the 8-K without 5.02 and the 8-K/A.
    assert ids(signals) == ALL_IDS
    assert ctx.warnings == [f'SEC EDGAR search "{QUERY}" (Form D): skipped 1 malformed hit(s)']
    assert all(r.lead is None and r.account_domain == "" for r in signals)
    assert all(r.signal.source == "sec_edgar" for r in signals)
    assert all(r.signal.occurred_at >= datetime(2026, 9, 24, tzinfo=UTC) for r in signals)
    assert all(len(r.signal.summary) <= 500 and "<" not in r.signal.summary for r in signals)


async def test_form_d_signal_fields(company):
    signals, _ = await run(configure(company), FakeEdgar())
    raw = by_id(signals)[D_FREIGHTWISE]
    s = raw.signal
    assert raw.account == "Freightwise Robotics, Inc."
    assert s.type == "funding"
    assert s.title == "Form D filing: Freightwise Robotics, Inc."
    assert s.url == "https://www.sec.gov/Archives/edgar/data/2091234/000209123426000001/"
    assert s.occurred_at == datetime(2026, 9, 29, tzinfo=UTC)
    assert s.strength == 60
    assert s.raw == {"form": "D", "adsh": "0002091234-26-000001", "cik": "2091234",
                     "biz_locations": ["Austin, TX"], "inc_states": ["DE"], "exemptions": ["06B"],
                     "queries": [QUERY]}
    assert s.summary == (
        "Freightwise Robotics, Inc. filed a Form D on 2026-09-29: notice of an exempt private securities "
        "offering, usually a new funding round under Rule 506(b). Based in Austin, TX, incorporated in DE. "
        'Matched SEC full-text search "logistics software". The filing lists the amount raised and the '
        "executive officers and directors.")
    haulmatic = by_id(signals)[D_HAULMATIC]
    assert haulmatic.account == "Haulmatic Labs LLC"
    assert "Rule 506(c)" in haulmatic.signal.summary


async def test_8k_signal_fields(company):
    signals, _ = await run(configure(company), FakeEdgar())
    raw = by_id(signals)[K_ROUTELOGIC]
    s = raw.signal
    assert raw.account == "Routelogic Holdings Inc."
    assert s.type == "job_change"
    assert s.title == "Leadership change (8-K 5.02): Routelogic Holdings Inc."
    assert s.url == "https://www.sec.gov/Archives/edgar/data/1843714/000119312526212345/"
    assert s.occurred_at == datetime(2026, 9, 30, tzinfo=UTC)
    assert s.strength == 55
    assert s.raw == {"form": "8-K", "adsh": "0001193125-26-212345", "cik": "1843714",
                     "biz_locations": ["Boston, MA"], "inc_states": ["DE"], "items": ["5.02", "9.01"],
                     "tickers": ["RTLG", "RTLGW"], "period_ending": "2026-09-28", "sics": ["7372"],
                     "queries": [QUERY]}
    assert s.summary.startswith(
        "Routelogic Holdings Inc. (RTLG, RTLGW) filed an 8-K on 2026-09-30 reporting Item 5.02: departure, "
        "election or appointment of directors or certain officers. Also reported: Item 9.01. Based in Boston, MA")
    assert s.summary.endswith("The people involved are named in the filing text.")
    # Found only through its EX-99.1 exhibit hit; "/DE/" and the ticker suffix are stripped from the name.
    fw = by_id(signals)[K_FREIGHTWISE]
    assert fw.account == "Freightwise Logistics Corp"
    assert fw.signal.url == "https://www.sec.gov/Archives/edgar/data/923456/000092345626000017/"
    assert fw.signal.raw["tickers"] == ["FWLC"]


@pytest.mark.parametrize(("display", "expected"), [
    ("Freightwise Robotics, Inc.  (CIK 0002091234)", ("Freightwise Robotics, Inc.", [], "0002091234")),
    ("Zapata Computing Holdings Inc. (ZPTA, ZPTAW) (CIK 0001843714)",
     ("Zapata Computing Holdings Inc.", ["ZPTA", "ZPTAW"], "0001843714")),
    ("ORACLE CORP /DE/ (ORCL) (CIK 0001341439)", ("ORACLE CORP", ["ORCL"], "0001341439")),
    ("Capria Opportunities, LP - Eduvanz Series  (CIK 0002052988)",
     ("Capria Opportunities, LP - Eduvanz Series", [], "0002052988")),
    ("Smith &amp; Rowe Freight, Inc.  (CIK 0002095555)", ("Smith & Rowe Freight, Inc.", [], "0002095555")),
    ("Plain Name", ("Plain Name", [], "")),
    ("", ("", [], "")),
])
def test_split_display_name(display, expected):
    assert split_display_name(display) == expected


@pytest.mark.parametrize(("name", "items", "fund"), [
    ("Capria Opportunities, LP - Eduvanz Series", ["06B"], True),
    ("Northbeam Logistics Fund II, LLC", ["06C"], True),
    ("Cargo AI SPV 2026, a Series of Roll Up Vehicles, LLC", ["06B"], True),
    ("XYZ Ventures III, L.P.", ["06B"], True),
    ("Acme Capital Partners III", ["06B"], True),
    ("Acme Co-Invest 2026 LLC", ["06B"], True),
    ("Quiet Holdings LLC", ["06B", "3C", "3C.7"], True),     # 3(c)(7) exclusion: a private fund
    ("Freightwise Robotics, Inc.", ["06B"], False),
    ("Helpful Robots Inc", ["06B"], False),
    ("LPL Robotics Inc", ["06B"], False),
    ("Series Entertainment Inc", ["06B"], False),
    ("Acme Ventures LLC", ["06C"], False),                   # could be a startup: kept (conservative)
])
def test_pooled_fund_heuristic(name, items, fund):
    assert is_pooled_fund(name, items) is fund


# --------------------------------------------------------------------------------------
# Time window, de-duplication, caps
# --------------------------------------------------------------------------------------


async def test_since_filter_is_day_precise(company):
    handler = FakeEdgar(routes={(Q, "D", 0): FORM_D_FIXTURE, (Q, "8-K", 0): EIGHT_K_FIXTURE})
    # Filings carry a date only: a filing made on since's day counts, older ones never do,
    # even if the server returned them.
    signals, _ = await run(configure(company), handler, since=datetime(2026, 9, 29, 18, 0, tzinfo=UTC))
    assert ids(signals) == [D_FREIGHTWISE, D_HAULMATIC, K_ROUTELOGIC, K_FREIGHTWISE]
    assert {r.url.params["startdt"] for r in handler.requests} == {"2026-09-29"}

    signals, _ = await run(configure(company), FakeEdgar(), since=datetime(2026, 10, 3, tzinfo=UTC))
    assert ids(signals) == [K_FREIGHTWISE]


async def test_naive_since_is_treated_as_utc(company):
    handler = FakeEdgar()
    signals, _ = await run(configure(company), handler, since=datetime(2026, 9, 30, 23, 0))
    assert {r.url.params["startdt"] for r in handler.requests} == {"2026-09-30"}
    assert ids(signals) == [D_HAULMATIC, K_ROUTELOGIC, K_FREIGHTWISE]


async def test_filing_found_by_several_queries_is_returned_once(company):
    second = '"freight software"'
    routes = {**default_routes(), (second, "D", 0): FORM_D_FIXTURE}
    signals, _ = await run(configure(company, sec_queries=[QUERY, "freight software"]), FakeEdgar(routes=routes))
    assert ids(signals) == ALL_IDS
    found = by_id(signals)
    assert found[D_FREIGHTWISE].signal.raw["queries"] == [QUERY, "freight software"]
    assert found[K_ROUTELOGIC].signal.raw["queries"] == [QUERY]
    assert '"freight software"' in found[D_FREIGHTWISE].signal.summary


async def test_external_ids_are_stable_across_scans(company):
    first, _ = await run(configure(company), FakeEdgar())
    again, _ = await run(configure(company), FakeEdgar())
    assert ids(first) == ids(again)
    assert len(set(ids(first))) == len(first)


async def test_max_items_stops_fetching(company):
    handler = FakeEdgar()
    signals, ctx = await run(configure(company), handler, max_items=2)
    assert ids(signals) == [D_FREIGHTWISE, D_PALLETLY]
    assert handler.searches == [(Q, "D", None)]
    assert ctx.warnings == ["SEC EDGAR: reached the limit of 2 signals; 1 search(es) not run this scan"]


async def test_max_items_zero_fetches_nothing(company):
    handler = FakeEdgar()
    signals, ctx = await run(configure(company), handler, max_items=0)
    assert signals == [] and handler.requests == [] and ctx.warnings == []


async def test_hits_per_search_are_capped(company):
    hits = [make_hit(n) for n in range(1, 61)]
    handler = FakeEdgar(routes={(Q, "D", 0): page(hits, total=250)})
    signals, _ = await run(configure(company, enabled_types=["funding"]), handler)
    assert len(signals) == sec_edgar.MAX_FILINGS_PER_SEARCH == 40
    assert handler.searches == [(Q, "D", None)]    # cap reached on page 1: no second page


async def test_second_page_is_fetched_with_from_offset(company):
    routes = {(Q, "D", 0): page([make_hit(n) for n in range(1, 4)], total=6),
              (Q, "D", 3): page([make_hit(n) for n in range(4, 7)], total=6)}
    handler = FakeEdgar(routes=routes)
    signals, _ = await run(configure(company, enabled_types=["funding"]), handler)
    assert handler.searches == [(Q, "D", None), (Q, "D", "3")]
    assert len(signals) == 6
    assert dict(handler.requests[1].url.params) == {"q": Q, "forms": "D", "dateRange": "custom",
                                                    "startdt": "2026-09-24", "enddt": "2026-10-08", "from": "3"}


async def test_query_and_request_caps(company):
    queries = [f"topic {n}" for n in range(1, 8)]

    def always_more(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("from", "0"))
        form = request.url.params["forms"]
        n = len(handler.requests) * 10 + offset
        return httpx.Response(200, json=page([make_hit(n, form=form)], total=500))

    handler = FakeEdgar(routes={}, default=always_more)
    signals, ctx = await run(configure(company, sec_queries=queries), handler)
    searched = [q for q, _, page_from in handler.searches if page_from is None]
    # 7 queries configured: 5 per scan (rotated daily), both forms each, every search gets its first page,
    # second pages only while they do not crowd out a later search, never more than the request cap.
    assert len(set(searched)) == sec_edgar.MAX_QUERIES == 5
    assert len(searched) == 10
    assert len(handler.requests) == sec_edgar.MAX_REQUESTS_PER_SCAN == 15
    assert ctx.warnings == [("SEC EDGAR: 7 queries configured but only 5 are searched per scan; "
                             "the searched set rotates daily")]
    assert len(signals) == 15

    # The next day's scan starts the rotation one query further.
    next_day = FakeEdgar(routes={}, default=EMPTY)
    await run(configure(company, sec_queries=queries), next_day, since=datetime(2026, 9, 25, tzinfo=UTC))
    assert [q for q, _, _ in next_day.searches[::2]] != searched[::2]


async def test_time_budget_stops_before_requests(company):
    handler = FakeEdgar()
    signals, ctx = await run(configure(company), handler, time_budget=0)
    assert signals == [] and handler.requests == []
    assert ctx.warnings == ["SEC EDGAR: time budget for this scan used up; 2 search(es) not run this scan"]


# --------------------------------------------------------------------------------------
# Errors: warnings, never exceptions
# --------------------------------------------------------------------------------------


async def test_403_html_stops_all_sec_requests(company):
    blocked = httpx.Response(403, text=(FIXTURES / "undeclared_tool_403.html").read_text(),
                             headers={"Content-Type": "text/html"})
    handler = FakeEdgar(routes={}, default=blocked)
    signals, ctx = await run(configure(company, sec_queries=["a", "b"]), handler)
    assert signals == []
    assert len(handler.requests) == 1
    assert len(ctx.warnings) == 1 and "HTTP 403" in ctx.warnings[0] and "contact e-mail" in ctx.warnings[0]


async def test_429_stops_and_reports_retry_after(company):
    limited = httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "600"})
    handler = FakeEdgar({(Q, "8-K", 0): limited})
    signals, ctx = await run(configure(company, sec_queries=[QUERY, "other"]), handler)
    assert ids(signals) == [D_FREIGHTWISE, D_PALLETLY, D_HAULMATIC]   # what came before the 429 is kept
    assert handler.searches == [(Q, "D", None), (Q, "8-K", None)]
    assert ctx.warnings[-1] == "SEC EDGAR rate limit hit (HTTP 429, retry after 600); no more SEC requests this scan"


async def test_rate_limit_header_stops_after_the_page(company):
    exhausted = httpx.Response(200, json=fixture(FORM_D_FIXTURE), headers={"X-RateLimit-Remaining": "0"})
    handler = FakeEdgar({(Q, "D", 0): exhausted})
    signals, ctx = await run(configure(company), handler)
    assert ids(signals) == [D_FREIGHTWISE, D_PALLETLY, D_HAULMATIC]
    assert len(handler.requests) == 1
    assert "SEC EDGAR: rate-limit budget exhausted; no more SEC requests this scan" in ctx.warnings


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("read timed out", request=request)


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("name resolution failed", request=request)


@pytest.mark.parametrize(("reply", "warning"), [
    (httpx.Response(500, text="<html>Internal Server Error</html>"), 'SEC EDGAR search "logistics software" (Form D): HTTP 500'),
    (httpx.Response(200, text="{not json", headers={"Content-Type": "application/json"}),
     'SEC EDGAR search "logistics software" (Form D): response was not valid JSON'),
    (httpx.Response(200, json={"error": "query failed"}),
     'SEC EDGAR search "logistics software" (Form D): unexpected response format'),
    (_timeout, 'SEC EDGAR search "logistics software" (Form D): request timed out'),
    (_connect_error, 'SEC EDGAR search "logistics software" (Form D): request failed (ConnectError)'),
])
async def test_failed_request_warns_and_continues(company, reply, warning):
    handler = FakeEdgar({(Q, "D", 0): reply})
    signals, ctx = await run(configure(company), handler)
    assert ids(signals) == [K_ROUTELOGIC, K_FREIGHTWISE]
    assert handler.searches == [(Q, "D", None), (Q, "8-K", None)]
    assert ctx.warnings == [warning]


async def test_three_failures_in_a_row_stop_the_scan(company):
    handler = FakeEdgar(routes={}, default=httpx.Response(503, text="Service Unavailable"))
    signals, ctx = await run(configure(company, sec_queries=["a", "b", "c"]), handler)
    assert signals == []
    assert len(handler.requests) == 3
    assert ctx.warnings[-1] == "SEC EDGAR: 3 failed requests in a row; no more SEC requests this scan"


async def test_malformed_hits_are_skipped(company):
    good = make_hit(1)
    bad = [
        "not a hit",
        {"_id": "x"},                                                     # no _source
        {"_source": {**good["_source"], "adsh": "", "file_date": "2026-10-01"}, "_id": "nope"},
        {"_source": {**good["_source"], "adsh": "0002100002-26-000002", "file_date": "yesterday"}},
        {"_source": {**good["_source"], "adsh": "0002100003-26-000003", "display_names": [], "ciks": []}},
        {"_source": {**good["_source"], "adsh": "0002100004-26-000004", "items": None, "biz_locations": "Reno, NV"}},
    ]
    handler = FakeEdgar(routes={(Q, "D", 0): page([*bad, good])})
    signals, ctx = await run(configure(company, enabled_types=["funding"]), handler)
    # The last "bad" hit only has odd optional fields: it is still a valid filing.
    assert ids(signals) == ["sec_edgar:D:0002100004-26-000004", "sec_edgar:D:0002100001-26-000001"]
    assert by_id(signals)["sec_edgar:D:0002100004-26-000004"].signal.raw["biz_locations"] == ["Reno, NV"]
    assert ctx.warnings == [f'SEC EDGAR search "{QUERY}" (Form D): skipped 5 malformed hit(s)']


async def test_adsh_falls_back_to_the_hit_id(company):
    hit = make_hit(7)
    del hit["_source"]["adsh"]
    signals, ctx = await run(configure(company, enabled_types=["funding"]),
                             FakeEdgar(routes={(Q, "D", 0): page([hit])}))
    assert ids(signals) == ["sec_edgar:D:0002100007-26-000007"]
    assert ctx.warnings == []


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------


async def test_ingest_creates_scored_account_leads(company):
    configured = configure(company)
    signals, _ = await run(configured, FakeEdgar())
    stats = ingest(company.id, signals)
    assert stats.errors == []
    assert stats.signals_new == 5 and stats.leads_new == 5

    accounts, total = repo.list_leads(company.id, kind="account")
    assert total == 5
    assert {lead.lead_company for lead in accounts} == {
        "Freightwise Robotics, Inc.", "Palletly, Inc.", "Haulmatic Labs LLC",
        "Routelogic Holdings Inc.", "Freightwise Logistics Corp"}
    assert all(lead.source == "sec_edgar" and lead.intent_score > 0 and lead.score > 0 for lead in accounts)
    stored, n = repo.list_signals(company.id, source="sec_edgar")
    assert n == 5 and {s.type for s in stored} == {"funding", "job_change"}

    # A person found later at Routelogic inherits the company-level leadership-change intent.
    person, _ = repo.upsert_lead(company.id, LeadIn(full_name="Dana Reyes", title="Travel Manager",
                                                    lead_company="Routelogic Holdings"))
    person = repo.get_lead(person.id)
    assert person.intent_score > 0
    assert any("(company)" in reason for reason in person.score_reasons)

    # The next scan sees the same filings: nothing new.
    again = ingest(company.id, (await run(configured, FakeEdgar()))[0])
    assert again.signals_new == 0 and again.signals_duplicate == 5 and again.leads_new == 0


async def test_run_scan_uses_the_collector(company, monkeypatch):
    from openberry.services import run_scan

    monkeypatch.setattr(SecEdgarCollector, "request_interval", 0)
    repo.update_company(company.id, {"contact_email": EMAIL, "signals": {"sec_queries": [QUERY]}})
    handler = FakeEdgar()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stats = await run_scan(company.id, sources=["sec_edgar"], client=client)
    assert stats["status"] == "ok"
    assert handler.searches == [(Q, "D", None), (Q, "8-K", None)]
    assert {r.headers["User-Agent"] for r in handler.requests} == {f"OpenBerry {EMAIL}"}
    sec_stats = stats["collectors"]["sec_edgar"]
    assert "error" not in sec_stats
    # run_scan looks back from the real clock, so how many fixture filings are recent depends on today.
    assert stats["signals_new"] == sec_stats["found"]
