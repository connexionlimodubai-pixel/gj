"""End to end: one scan runs every collector together and turns the results into scored leads.

Each collector's own test module provides a fake API (recorded payloads from the documented
APIs); this test routes one shared httpx client to all of them by host.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from openberry import repo, services
from openberry.collectors import ALL, news, sec_edgar

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

    async def public(_url: str) -> None:
        return None

    monkeypatch.setattr(news, "assert_public_host", public)
    for collector in ALL:
        monkeypatch.setattr(type(collector), "request_interval", 0, raising=False)
    monkeypatch.setattr(settings, "reddit_client_id", "id")
    monkeypatch.setattr(settings, "reddit_client_secret", "secret")
    monkeypatch.setattr(settings, "contact_email", "ops@acme.example")
    return repo.update_company(company.id, {
        "signals": {
            "rss_feeds": [NEWS.TC_FEED],
            "news_queries": [NEWS.DUBAI, NEWS.SERIES_A],
            "sec_queries": ["logistics software"],
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
                                 "api.lever.co", "news.google.com", "techcrunch.com", "efts.sec.gov"}

    assert stats["signals_new"] == sum(found.values()) - stats["signals_duplicate"]
    leads, total = repo.list_leads(company.id, limit=500)
    assert total == stats["leads_new"] > 0
    people = [lead for lead in leads if lead.kind == "person"]
    accounts = [lead for lead in leads if lead.kind == "account"]
    assert people and accounts
    assert all(lead.score_reasons for lead in leads)
    assert {s["source"] for s in repo.company_stats(company.id, now=NOW)["signals_by_source"]} >= {
        "hackernews", "reddit", "github", "greenhouse", "lever", "google_news", "rss", "sec_edgar"}

    # New hot leads get a first-touch draft in auto_draft mode; nothing is ever marked sent.
    drafts = repo.list_messages(company.id)
    assert len(drafts) == stats["drafted"] == len(stats["newly_hot"])
    assert all(m.status == "draft" for m in drafts)

    # Scanning again with the same data creates nothing new.
    async with httpx.AsyncClient(transport=httpx.MockTransport(Router())) as client:
        again = await services.run_scan(company.id, client=client)
    assert again["signals_new"] == 0 and again["leads_new"] == 0
    assert repo.list_leads(company.id, limit=500)[1] == total
