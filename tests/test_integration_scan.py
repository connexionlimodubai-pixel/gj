"""End to end: one scan runs every collector together and turns the results into scored leads.

Each collector's own test module provides a fake API (recorded payloads from the documented
APIs); this test routes one shared httpx client to all of them by host.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from openberry import envfile, repo, services
from openberry.collectors import ALL, google_places, news, sec_edgar
from openberry.models import LeadIn
from places_fakes import ACME, DESERT, GOOGLE_KEY, QUERY, FakeGoogle, FakeSites, go_offline

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
HERE = Path(__file__).parent


def _load(name: str):
    module_name = f"_openberry_it_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, HERE / f"test_collector_{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


HN, REDDIT, GITHUB, JOBS, NEWS, SEC = (_load(n) for n in ("hackernews", "reddit", "github", "jobs", "news", "sec_edgar"))


class Router:
    """Send each request to the fake API that owns its host."""

    def __init__(self) -> None:
        self.fakes = {
            "hn.algolia.com": HN.FakeAlgolia(),
            "www.reddit.com": (reddit := REDDIT.FakeReddit()),
            "oauth.reddit.com": reddit,
            "api.github.com": GITHUB.FakeGitHub(),
            "boards-api.greenhouse.io": (boards := JOBS.FakeBoards()),
            "api.lever.co": boards,
            "api.ashbyhq.com": boards,
            "news.google.com": (web := NEWS.FakeWeb()),
            "techcrunch.com": web,
            "efts.sec.gov": SEC.FakeEdgar(),
            "places.googleapis.com": FakeGoogle(),
        }
        self.hosts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        fake = self.fakes.get(request.url.host)
        return fake(request) if fake else httpx.Response(404, text="unexpected host in test")


@pytest.fixture
def scan_ready(company, settings, monkeypatch):
    """The conftest company with every source configured, a fixed clock and no politeness delays."""
    monkeypatch.setattr(repo, "utcnow", lambda: NOW)
    monkeypatch.setattr(news, "_utcnow", lambda: NOW)
    monkeypatch.setattr(sec_edgar, "_utcnow", lambda: NOW)
    monkeypatch.setattr(google_places, "_utcnow", lambda: NOW)
    go_offline(monkeypatch, FakeSites())  # business websites found on Google Maps

    async def public(_url: str) -> None:
        return None

    monkeypatch.setattr(news, "assert_public_host", public)
    # user feeds normally get their own address-checking client; route them through the mock instead
    monkeypatch.setattr(news, "_feed_client", lambda ctx, public_only: contextlib.nullcontext(ctx.client))
    for collector in ALL:
        monkeypatch.setattr(type(collector), "request_interval", 0, raising=False)
    monkeypatch.setattr(settings, "reddit_client_id", "id")
    monkeypatch.setattr(settings, "reddit_client_secret", "secret")
    monkeypatch.setattr(settings, "contact_email", "ops@acme.example")
    monkeypatch.setattr(settings, "google_places_key", GOOGLE_KEY)
    return repo.update_company(company.id, {
        "signals": {
            "rss_feeds": [NEWS.TC_FEED],
            "news_queries": [NEWS.DUBAI, NEWS.SERIES_A],
            "sec_queries": ["logistics software"],
            "places_queries": [QUERY],
            "lookback_days": 14,
        },
        "notify": {"slack_webhook_url": "", "min_score": 0},
        "outreach": {"mode": "auto_draft"},
    })


async def test_one_scan_runs_every_source_and_scores_leads(scan_ready):
    company = scan_ready
    router = Router()
    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        stats = await services.run_scan(company.id, client=client)

    assert stats["status"] == "ok", stats
    assert stats["skipped"] == []
    for name, result in stats["collectors"].items():
        assert "error" not in result, (name, result)
    found = {name: result["found"] for name, result in stats["collectors"].items()}
    assert all(n > 0 for n in found.values()), found
    assert set(router.hosts) >= {"hn.algolia.com", "oauth.reddit.com", "api.github.com", "boards-api.greenhouse.io",
                                 "api.lever.co", "news.google.com", "techcrunch.com", "efts.sec.gov",
                                 "places.googleapis.com"}
    assert stats["collectors"]["google_places"]["counts"]["added"] == 6

    assert stats["signals_new"] == sum(found.values()) - stats["signals_duplicate"]
    leads, total = repo.list_leads(company.id, limit=500)
    assert total == stats["leads_new"] > 0
    people = [lead for lead in leads if lead.kind == "person"]
    accounts = [lead for lead in leads if lead.kind == "account"]
    assert people and accounts
    assert all(lead.score_reasons for lead in leads)
    assert {s["source"] for s in repo.company_stats(company.id, now=NOW)["signals_by_source"]} >= {
        "hackernews", "reddit", "github", "greenhouse", "lever", "google_news", "rss", "sec_edgar", "google_places"}

    # New hot leads get a first-touch draft in auto_draft mode; nothing is ever marked sent.
    drafts = repo.list_messages(company.id)
    assert len(drafts) == stats["drafted"] == len(stats["newly_hot"])
    assert all(m.status == "draft" for m in drafts)

    # Scanning again with the same data creates nothing new.
    async with httpx.AsyncClient(transport=httpx.MockTransport(Router())) as client:
        again = await services.run_scan(company.id, client=client)
    assert again["signals_new"] == 0 and again["leads_new"] == 0
    assert repo.list_leads(company.id, limit=500)[1] == total


# --------------------------------------------------------------------------------------
# Google Maps businesses: merging with what other sources found
# --------------------------------------------------------------------------------------


@pytest.fixture
def maps_ready(company, settings, monkeypatch):
    """The conftest company with only Google Maps searches, a key, and a clock the test can move."""
    clock = {"now": NOW}
    monkeypatch.setattr(repo, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(google_places, "_utcnow", lambda: clock["now"])
    go_offline(monkeypatch, FakeSites())
    monkeypatch.setattr(settings, "google_places_key", GOOGLE_KEY)
    company = repo.update_company(company.id, {"signals": {"places_queries": [QUERY]}})
    return company, clock


async def scan_maps(company_id: int) -> dict:
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle())) as client:
        return await services.run_scan(company_id, sources=["google_places"], client=client)


def lead_named(company_id: int, name: str):
    return next(lead for lead in repo.list_leads(company_id, limit=500)[0] if lead.lead_company == name)


async def test_google_maps_businesses_merge_with_existing_leads(maps_ready):
    company, clock = maps_ready
    hiring, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Acme Events", source="greenhouse"))
    by_domain, _ = repo.upsert_lead(company.id, LeadIn(company_domain="desertdmc.com", source="csv"))
    person, _ = repo.upsert_lead(company.id, LeadIn(full_name="Sara Ahmed", title="Events Director",
                                                    lead_company="Acme Events"))

    stats = await scan_maps(company.id)

    assert stats["status"] == "ok" and stats["leads_new"] == 4 and stats["leads_updated"] == 2
    assert stats["collectors"]["google_places"]["counts"]["added"] == 6
    acme = repo.get_lead(hiring.id)
    assert (acme.company_domain, acme.email, acme.phone, acme.website, acme.source) == (
        "acme-events.ae", "info@acme-events.ae", "+97145550101", "https://www.acme-events.ae/", "greenhouse")
    assert acme.profile_url == repo.maps_place_url(ACME)
    desert = repo.get_lead(by_domain.id)
    assert desert.lead_company == "Desert DMC" and desert.email == "info@desertdmc.com"
    # The person at Acme Events inherits a little of the account's (small) intent.
    sara = repo.get_lead(person.id)
    assert any("Matches a business search you set up (company)" in r for r in sara.score_reasons)
    new = lead_named(company.id, "Gulf Law Partners")
    assert new.kind == "account" and new.location == "Dubai" and new.tier != "hot"

    # A week later the search runs again: the same businesses give no new leads or signals.
    clock["now"] = NOW + timedelta(days=8)
    total = repo.list_leads(company.id, limit=500)[1]
    again = await scan_maps(company.id)
    assert again["status"] == "ok" and again["leads_new"] == 0 and again["signals_new"] == 0
    assert again["collectors"]["google_places"]["counts"]["already_handled"] == 10  # every business, no visit
    assert repo.list_leads(company.id, limit=500)[1] == total


async def test_a_key_saved_from_the_dashboard_works_without_a_restart(maps_ready, settings, monkeypatch):
    company, _ = maps_ready
    monkeypatch.setattr(settings, "google_places_key", "")
    assert (await scan_maps(company.id))["status"] == "nothing_configured"
    monkeypatch.setenv("OPENBERRY_GOOGLE_PLACES_KEY", "")
    monkeypatch.delenv("OPENBERRY_GOOGLE_PLACES_KEY")  # removed again after the test
    envfile.save_settings({"OPENBERRY_GOOGLE_PLACES_KEY": GOOGLE_KEY})
    stats = await scan_maps(company.id)
    assert stats["status"] == "ok" and stats["leads_new"] == 6
    assert DESERT in {s.external_id.removeprefix("gp:") for s in repo.list_signals(company.id)[0]}
