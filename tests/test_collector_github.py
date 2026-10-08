"""GitHub collector (REST API: issues, forks, stargazers, users), fully offline via httpx.MockTransport.

Fixtures in tests/fixtures/github mirror the documented GitHub payloads from the API research:
issue/PR list items, fork repository objects, star+json stargazer entries, public user profiles
and GitHub's JSON error bodies (plus an HTML 403 page).
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
from openberry.collectors import github as gh
from openberry.collectors.base import CollectContext, RawSignal
from openberry.collectors.github import GitHubCollector
from openberry.config import get_settings
from openberry.models import Company
from openberry.services import ingest

FIXTURES = Path(__file__).parent / "fixtures" / "github"
SINCE = datetime(2026, 9, 24, tzinfo=timezone.utc)
API = "https://api.github.com"
REPO = "acme/limo-sdk"
ISSUES = f"/repos/{REPO}/issues"
FORKS = f"/repos/{REPO}/forks"
STARS = f"/repos/{REPO}/stargazers"
TOKEN = "ghp_testtoken123"
STARS_LINK = (
    '<https://api.github.com/repositories/812345678/stargazers?per_page=100&page=2>; rel="next", '
    '<https://api.github.com/repositories/812345678/stargazers?per_page=100&page=3>; rel="last"'
)

ISSUE_IDS = {"github:issue:3456700212", "github:issue:3456700210", "github:issue:3456700208"}
FORK_IDS = {"github:fork:912340001", "github:fork:912340002", "github:fork:912340003"}
STAR_IDS = {"github:star:acme/limo-sdk:50334455", "github:star:acme/limo-sdk:50667788"}
ANON_IDS = ISSUE_IDS | FORK_IDS
TOKEN_IDS = ANON_IDS | STAR_IDS

Override = httpx.Response | Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gh_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    base = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "1791453600",
            "X-RateLimit-Used": "18", "X-RateLimit-Resource": "core"}
    return httpx.Response(status, json=payload, headers={**base, **(headers or {})})


def error(name: str, status: int, **headers: str) -> httpx.Response:
    return gh_response(status, fixture(name), headers)


class FakeGitHub:
    """MockTransport handler: serves fixtures by path (and page), records requests, injects failures."""

    def __init__(self, overrides: dict[str | tuple[str, str | None], Override] | None = None,
                 extra_forks: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides = overrides or {}
        self.extra_forks = extra_forks or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        path, page = request.url.path, request.url.params.get("page")
        for key in ((path, page), path):
            if key in self.overrides:
                override = self.overrides[key]
                return override(request) if callable(override) else override
        return self.route(path, page)

    def route(self, path: str, page: str | None) -> httpx.Response:
        if path == ISSUES:
            return gh_response(200, fixture("issues_acme_limo-sdk.json"))
        if path == FORKS:
            return gh_response(200, fixture("forks_acme_limo-sdk.json"))
        if path == STARS:
            if page is None:
                return gh_response(200, fixture("stargazers_page1.json"), {"Link": STARS_LINK})
            return gh_response(200, fixture(f"stargazers_page{page}.json"))
        if path.startswith("/users/"):
            user = FIXTURES / f"user_{path.removeprefix('/users/')}.json"
            return gh_response(200, json.loads(user.read_text())) if user.is_file() else error("error_404.json", 404)
        if path.endswith("/forks"):
            return gh_response(200, self.extra_forks.get(path, []))
        if path.endswith(("/issues", "/stargazers")):
            return gh_response(200, [])
        return error("error_404.json", 404)

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def user_lookups(self) -> list[str]:
        return [p.removeprefix("/users/") for p in self.paths if p.startswith("/users/")]


async def run(company: Company, handler: FakeGitHub, *, since: datetime = SINCE, max_items: int = 200,
              time_budget: float = 60.0) -> tuple[list[RawSignal], CollectContext]:
    collector = GitHubCollector()
    collector.request_interval = 0
    collector.time_budget = time_budget
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"User-Agent": "OpenBerry-test"}) as client:
        ctx = CollectContext(client=client, since=since, settings=get_settings(), max_items=max_items)
        signals = await collector.collect(company, ctx)
    return signals, ctx


def by_id(signals: list[RawSignal]) -> dict[str, RawSignal]:
    return {r.signal.external_id: r for r in signals}


def configure(company: Company, **signals: Any) -> Company:
    return company.model_copy(update={"signals": company.signals.model_copy(update=signals)})


def simple_user(login: str, uid: int, kind: str = "User") -> dict[str, Any]:
    return {"login": login, "id": uid, "html_url": f"https://github.com/{login}", "type": kind,
            "user_view_type": "public", "site_admin": False}


def make_fork(owner: str, uid: int, fid: int, created: str = "2026-10-02T10:00:00Z") -> dict[str, Any]:
    return {"id": fid, "name": "limo-sdk", "full_name": f"{owner}/limo-sdk", "owner": simple_user(owner, uid),
            "html_url": f"https://github.com/{owner}/limo-sdk", "fork": True, "created_at": created,
            "updated_at": created, "pushed_at": "2026-09-30T10:00:00Z", "description": None}


@pytest.fixture
def token(settings):
    settings.github_token = TOKEN
    return settings


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_registered_with_declared_metadata():
    collector = COLLECTORS["github"]
    assert isinstance(collector, GitHubCollector)
    assert collector.signal_types == ("competitor_engagement", "github_star")
    assert collector.label == "GitHub issues, forks & stars"


def test_is_configured(company):
    collector = GitHubCollector()
    assert collector.is_configured(company)
    bare = configure(company, github_repos=[])
    assert not collector.is_configured(bare)
    assert not collector.enabled_for(bare)
    assert collector.enabled_for(configure(company, enabled_types=["github_star"]))
    assert collector.enabled_for(configure(company, enabled_types=["competitor_engagement"]))
    assert not collector.enabled_for(configure(company, enabled_types=["hiring", "funding"]))


def test_is_configured_does_not_need_a_token(company, settings):
    settings.github_token = ""
    assert GitHubCollector().is_configured(company)


# --------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------


async def test_sends_expected_requests_without_token(company):
    handler = FakeGitHub()
    signals, ctx = await run(company, handler)
    assert ctx.warnings == []
    assert handler.paths == [ISSUES, FORKS, "/users/dana-ops", "/users/kofi-dev", "/users/globex-travel",
                             "/users/lina-k", "/users/sam-builds"]
    issues, forks = handler.requests[0], handler.requests[1]
    assert str(issues.url).startswith(f"{API}{ISSUES}?")
    assert dict(issues.url.params) == {"state": "all", "since": "2026-09-24T00:00:00Z", "sort": "created",
                                       "direction": "desc", "per_page": "50"}
    assert dict(forks.url.params) == {"sort": "newest", "per_page": "30"}
    for request in handler.requests:
        assert request.method == "GET"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.headers["User-Agent"] == "OpenBerry-test"  # the shared client's UA is kept
        assert "Authorization" not in request.headers
        assert not request.url.params.get("page")
    assert STARS not in handler.paths  # stargazers need a token (and admin access)
    assert set(by_id(signals)) == ANON_IDS


async def test_token_sends_bearer_and_reads_newest_stargazer_page(company, token):
    handler = FakeGitHub()
    signals, ctx = await run(company, handler)
    assert ctx.warnings == []
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in handler.requests)
    star_requests = [r for r in handler.requests if r.url.path == STARS]
    assert [dict(r.url.params) for r in star_requests] == [{"per_page": "100"}, {"per_page": "100", "page": "3"}]
    assert all(r.headers["Accept"] == "application/vnd.github.star+json" for r in star_requests)
    assert handler.user_lookups() == ["dana-ops", "kofi-dev", "globex-travel", "mo-ahmed", "lina-k", "sam-builds"]
    assert set(by_id(signals)) == TOKEN_IDS

    star = by_id(signals)["github:star:acme/limo-sdk:50667788"]
    assert star.signal.type == "github_star" and star.signal.source == "github"
    assert star.signal.title == "Starred acme/limo-sdk"
    assert star.signal.url == "https://github.com/acme/limo-sdk/stargazers"
    assert star.signal.occurred_at == datetime(2026, 10, 4, 17, 20, tzinfo=timezone.utc)
    assert star.signal.strength == 50
    assert star.signal.raw == {"repo": REPO, "kind": "star", "user": "mo-ahmed"}
    assert star.lead.full_name == "Mo Ahmed" and star.lead.lead_company == "acmebank"
    assert star.lead.website == "http://mo.dev" and star.lead.github_username == "mo-ahmed"


async def test_stargazers_walk_back_while_the_newest_page_is_all_new(company, token):
    newest = [{"starred_at": "2026-10-07T09:00:00Z", "user": simple_user("fresh-star", 70000001)}]
    handler = FakeGitHub({(STARS, "3"): gh_response(200, newest)})
    signals, ctx = await run(company, handler)
    pages = [r.url.params.get("page") for r in handler.requests if r.url.path == STARS]
    assert pages == [None, "3", "2"]  # page 2 holds an older star, so page 1 is never re-read
    ids = set(by_id(signals))
    assert "github:star:acme/limo-sdk:70000001" in ids
    assert "github:star:acme/limo-sdk:20333444" not in ids  # 2026-08-20, before since
    assert ctx.warnings == []


async def test_single_stargazer_page_without_link_header(company, token):
    page = fixture("stargazers_page3.json")
    handler = FakeGitHub({STARS: gh_response(200, page)})
    signals, _ = await run(company, handler)
    assert [r.url.params.get("page") for r in handler.requests if r.url.path == STARS] == [None]
    assert STAR_IDS <= set(by_id(signals))


@pytest.mark.parametrize("denied", [
    error("error_404.json", 404),
    error("error_403_forbidden.json", 403),
    error("error_401_requires_auth.json", 401),
])
async def test_restricted_stargazers_warn_once_and_continue(company, token, denied):
    company = configure(company, github_repos=[REPO, "rival/fleet-api"])
    handler = FakeGitHub({STARS: denied, "/repos/rival/fleet-api/stargazers": denied})
    signals, ctx = await run(company, handler)
    star_warnings = [w for w in ctx.warnings if "stargazer" in w]
    assert len(star_warnings) == 1
    assert "only available for repos you administer" in star_warnings[0] and REPO in star_warnings[0]
    assert ctx.warnings == star_warnings
    assert handler.paths.count("/repos/rival/fleet-api/stargazers") == 1
    assert "/repos/rival/fleet-api/issues" in handler.paths  # other repos still scanned
    assert set(by_id(signals)) == ANON_IDS


async def test_enabled_types_limit_requests(company):
    stars_only = FakeGitHub()
    signals, _ = await run(configure(company, enabled_types=["github_star"]), stars_only)
    assert ISSUES not in stars_only.paths and FORKS in stars_only.paths
    assert {r.signal.type for r in signals} == {"github_star"}

    issues_only = FakeGitHub()
    signals, _ = await run(configure(company, enabled_types=["competitor_engagement"]), issues_only)
    assert FORKS not in issues_only.paths and ISSUES in issues_only.paths
    assert {r.signal.type for r in signals} == {"competitor_engagement"}


# --------------------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------------------


async def test_maps_issues_and_prs_to_competitor_engagement(company):
    signals = by_id((await run(company, FakeGitHub()))[0])

    issue = signals["github:issue:3456700212"]
    s = issue.signal
    assert s.type == "competitor_engagement" and s.source == "github"
    assert s.title == ("Opened an issue on acme/limo-sdk: "
                       "Migrating from Blacklane API: webhook for booking status changes?")
    assert s.url == "https://github.com/acme/limo-sdk/issues/212"
    assert s.occurred_at == datetime(2026, 10, 6, 9, 15, tzinfo=timezone.utc)
    assert s.strength == 80  # migration + evaluation intent
    assert s.summary.startswith("We're evaluating **limo-sdk** to replace our current provider")
    assert "Please search existing issues" not in s.summary  # issue-template comment removed
    assert s.raw["repo"] == REPO and s.raw["number"] == 212 and s.raw["kind"] == "issue"
    assert s.raw["author"] == "dana-ops" and s.raw["author_association"] == "NONE"
    assert s.raw["labels"] == ["question"] and s.raw["intent"] == ["evaluation"]
    assert {"migrating from", "evaluating"} <= set(s.raw["intent_phrases"])
    assert "body" not in s.raw and "user" not in s.raw  # not the whole payload

    lead = issue.lead
    assert issue.account == "" and lead is not None
    assert lead.full_name == "Dana Haddad"
    assert lead.github_username == "dana-ops"
    assert lead.profile_url == "https://github.com/dana-ops"
    assert lead.lead_company == "northwind-logistics"  # leading @ stripped
    assert lead.website == "https://northwind.example"  # scheme added
    assert lead.location == "Dubai, United Arab Emirates"
    assert lead.email == "dana@northwind.example"
    assert lead.twitter == "danahaddad"
    assert lead.bio.startswith("Travel Manager at Northwind Logistics")
    assert lead.source == "github"

    pr = signals["github:issue:3456700210"].signal
    assert pr.title == "Opened a PR on acme/limo-sdk: Add retry with exponential backoff to BookingClient"
    assert pr.url == "https://github.com/acme/limo-sdk/pull/210"
    assert pr.strength == 70 and pr.raw["kind"] == "pull_request" and pr.raw["intent"] == ["integration"]
    assert pr.raw["author_association"] == "FIRST_TIME_CONTRIBUTOR"
    kofi = signals["github:issue:3456700210"].lead
    assert kofi.full_name == "Kofi Mensah" and kofi.lead_company == "Globex Travel GmbH"
    assert kofi.website == "" and kofi.email == "" and kofi.twitter == "" and kofi.bio == ""

    plain = signals["github:issue:3456700208"]
    assert plain.signal.strength == 50  # "evaluating ... production" only appears in the template comment
    assert "intent" not in plain.signal.raw
    assert plain.signal.summary == ("Describe the bug The install section of the README says "
                                    "`pip instal limo-sdk` (missing an l).")
    assert plain.lead.full_name == "lina-k"  # no public name: login is used


async def test_maps_forks_to_github_star(company):
    signals = by_id((await run(company, FakeGitHub()))[0])

    active = signals["github:fork:912340001"]
    assert active.signal.type == "github_star" and active.signal.source == "github"
    assert active.signal.title == "Forked acme/limo-sdk"
    assert active.signal.url == "https://github.com/dana-ops/limo-sdk"
    assert active.signal.strength == 65  # pushed to the fork after forking
    assert active.signal.occurred_at == datetime(2026, 10, 6, 10, 2, tzinfo=timezone.utc)
    assert "has pushed to it since (last push 2026-10-07)" in active.signal.summary
    assert active.signal.raw["fork"] == "dana-ops/limo-sdk" and active.signal.raw["pushed_after_fork"] is True
    assert active.lead.github_username == "dana-ops" and active.lead.full_name == "Dana Haddad"

    org = signals["github:fork:912340002"]
    assert org.lead is None  # organisation fork -> account-level signal
    assert org.account == "Globex Travel" and org.account_domain == "globex-travel.example"
    assert org.signal.strength == 60 and org.signal.raw["owner_type"] == "Organization"

    plain = signals["github:fork:912340003"]
    assert plain.signal.strength == 50 and plain.signal.raw["pushed_after_fork"] is False
    assert plain.signal.summary.startswith("sam-builds forked acme/limo-sdk as sam-builds/limo-sdk.")
    lead = plain.lead  # profile 404 (deleted/renamed): login-only lead
    assert (lead.full_name, lead.github_username, lead.profile_url) == \
        ("sam-builds", "sam-builds", "https://github.com/sam-builds")
    assert lead.lead_company == lead.website == lead.location == lead.email == ""


async def test_skips_bots_insiders_and_old_items(company):
    handler = FakeGitHub()
    signals, _ = await run(company, handler)
    actors = {r.lead.github_username for r in signals if r.lead} | {r.account for r in signals if r.account}
    assert actors == {"dana-ops", "kofi-dev", "lina-k", "sam-builds", "Globex Travel"}
    for skipped in ("dependabot[bot]", "rahul-acme", "acme", "old-timer", "past-forker"):
        assert f"/users/{skipped}" not in handler.paths


async def test_bot_by_login_suffix_is_skipped(company):
    issue = fixture("issues_acme_limo-sdk.json")[0]
    issue["user"] = {**issue["user"], "login": "release-helper[bot]", "type": "User"}
    handler = FakeGitHub({ISSUES: gh_response(200, [issue])})
    signals, _ = await run(company, handler)
    assert not [r for r in signals if r.signal.type == "competitor_engagement"]


async def test_only_items_newer_than_since(company):
    since = datetime(2026, 10, 4, tzinfo=timezone.utc)
    handler = FakeGitHub()
    signals, _ = await run(company, handler, since=since)
    assert set(by_id(signals)) == {"github:issue:3456700212", "github:fork:912340001"}
    assert handler.requests[0].url.params["since"] == "2026-10-04T00:00:00Z"
    assert all(r.signal.occurred_at >= since for r in signals)


async def test_naive_since_is_treated_as_utc(company):
    signals, ctx = await run(company, FakeGitHub(), since=datetime(2026, 10, 4))  # noqa: DTZ001
    assert set(by_id(signals)) == {"github:issue:3456700212", "github:fork:912340001"}
    assert ctx.warnings == []


async def test_ids_are_stable_unique_and_duplicates_merge(company, token):
    first, _ = await run(company, FakeGitHub())
    second, _ = await run(company, FakeGitHub())
    ids = [r.signal.external_id for r in first]
    assert len(ids) == len(set(ids)) == len(TOKEN_IDS)
    assert ids == [r.signal.external_id for r in second]
    assert all(i.startswith("github:") for i in ids)

    issues = fixture("issues_acme_limo-sdk.json")
    handler = FakeGitHub({ISSUES: gh_response(200, issues + issues[:1])})  # page shifted between calls
    signals, _ = await run(company, handler)
    assert [r.signal.external_id for r in signals].count("github:issue:3456700212") == 1
    assert handler.user_lookups().count("dana-ops") == 1  # one profile lookup per person per scan


# --------------------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------------------


async def test_profile_lookups_are_capped_and_cached(company):
    forks = [make_fork(f"dev-{i:02d}", 80000000 + i, 990000000 + i) for i in range(40)]
    handler = FakeGitHub({FORKS: gh_response(200, forks)})
    signals, ctx = await run(company, handler)
    lookups = handler.user_lookups()
    assert len(lookups) == gh.MAX_PROFILES == 25
    assert len(set(lookups)) == len(lookups)
    # Strongest, then newest signals are enriched first: lina-k's older plain issue loses out.
    assert lookups[:3] == ["dana-ops", "kofi-dev", "dev-00"] and "lina-k" not in lookups
    assert any("looked up 25 of 43 profiles" in w for w in ctx.warnings)
    leads = [r.lead for r in signals if r.lead]
    assert len(leads) == 43
    assert all(lead.github_username and lead.profile_url for lead in leads)


async def test_repo_and_request_caps(company):
    repos = [f"rival{i}/sdk" for i in range(7)]
    extra = {f"/repos/rival{i}/sdk/forks": [make_fork(f"r{i}-dev{j}", 81000000 + i * 100 + j,
                                                     991000000 + i * 100 + j) for j in range(10)]
             for i in range(7)}
    handler = FakeGitHub(extra_forks=extra)
    signals, ctx = await run(configure(company, github_repos=repos), handler)
    listed = {p.split("/")[2] for p in handler.paths if p.startswith("/repos/")}
    assert listed == {f"rival{i}" for i in range(5)}
    assert any("only the first 5 of 7 repos" in w for w in ctx.warnings)
    assert len(handler.requests) == gh.MAX_REQUESTS_ANONYMOUS == 30  # 10 list calls + 20 profiles
    assert len(handler.user_lookups()) == 20
    assert len(signals) == 50


async def test_max_items_keeps_the_strongest_and_limits_lookups(company):
    handler = FakeGitHub()
    signals, _ = await run(company, handler, max_items=2)
    assert [r.signal.external_id for r in signals] == ["github:issue:3456700212", "github:issue:3456700210"]
    assert handler.user_lookups() == ["dana-ops", "kofi-dev"]


async def test_max_items_zero_makes_no_requests(company):
    handler = FakeGitHub()
    signals, _ = await run(company, handler, max_items=0)
    assert signals == [] and handler.requests == []


async def test_time_budget_stops_with_warning(company):
    handler = FakeGitHub()
    signals, ctx = await run(company, handler, time_budget=-1)
    assert signals == [] and handler.requests == []
    assert any("time budget" in w for w in ctx.warnings)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


async def test_403_html_stops_with_warning(company):
    blocked = httpx.Response(403, text=(FIXTURES / "blocked_403.html").read_text(),
                             headers={"Content-Type": "text/html; charset=utf-8"})
    handler = FakeGitHub({ISSUES: blocked})
    signals, ctx = await run(company, handler)
    assert signals == [] and handler.paths == [ISSUES]
    assert len(ctx.warnings) == 1 and "access refused (HTTP 403" in ctx.warnings[0]


async def test_primary_rate_limit_403_stops_with_reset_time_and_token_hint(company):
    limited = error("error_403_rate_limit.json", 403, **{"X-RateLimit-Remaining": "0",
                                                         "X-RateLimit-Reset": "1791453600"})
    handler = FakeGitHub({ISSUES: limited})
    signals, ctx = await run(company, handler)
    assert signals == [] and handler.paths == [ISSUES]
    assert ctx.warnings == [("GitHub: API rate limit used up (HTTP 403, resets 10:00 UTC); stopped this scan; "
                             "set GITHUB_TOKEN for 5,000 requests/hour")]


async def test_secondary_rate_limit_403_stops(company):
    handler = FakeGitHub({ISSUES: error("error_403_secondary.json", 403)})
    signals, ctx = await run(company, handler)
    assert signals == [] and handler.paths == [ISSUES]
    assert ctx.warnings == ["GitHub: secondary rate limit hit (HTTP 403); stopped this scan"]


async def test_429_stops_but_keeps_what_was_found(company):
    handler = FakeGitHub({FORKS: httpx.Response(429, json={"message": "Too many requests"},
                                                headers={"Retry-After": "60"})})
    signals, ctx = await run(company, handler)
    assert handler.paths == [ISSUES, FORKS]  # no profile lookups after a 429
    assert ctx.warnings == ["GitHub: secondary rate limit hit (HTTP 429, retry after 60s); stopped this scan"]
    assert set(by_id(signals)) == ISSUE_IDS
    dana = by_id(signals)["github:issue:3456700212"].lead
    assert (dana.full_name, dana.github_username) == ("dana-ops", "dana-ops")  # not enriched


async def test_rate_limit_remaining_zero_stops_after_this_response(company, token):
    handler = FakeGitHub({ISSUES: gh_response(200, fixture("issues_acme_limo-sdk.json"),
                                              {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1791453600"})})
    signals, ctx = await run(company, handler)
    assert handler.paths == [ISSUES]
    assert set(by_id(signals)) == ISSUE_IDS
    # No token hint: a token is set.
    assert ctx.warnings == [("GitHub: API rate limit used up (X-RateLimit-Remaining: 0, resets 10:00 UTC); "
                             "stopped this scan")]


@pytest.mark.parametrize("failure, message", [
    (httpx.Response(500, text="Internal Server Error"), "GitHub: HTTP 500 for issues of acme/limo-sdk"),
    (httpx.Response(502, json={"message": "Server Error"}),
     "GitHub: HTTP 502 for issues of acme/limo-sdk: Server Error"),
    (httpx.Response(200, text="{not json", headers={"Content-Type": "application/json"}),
     "GitHub: invalid JSON for issues of acme/limo-sdk"),
    (httpx.Response(200, json={"message": "Something odd"}),
     "GitHub: unexpected response for issues of acme/limo-sdk: Something odd"),
    (httpx.Response(301, json={"message": "Moved Permanently", "url": f"{API}/repositories/1/issues"},
                    headers={"Location": f"{API}/repositories/1/issues"}),
     "GitHub: issues of acme/limo-sdk was redirected (HTTP 301); was the repository renamed?"),
    (error("error_403_forbidden.json", 403),
     "GitHub: HTTP 403 for issues of acme/limo-sdk: Must have admin rights to Repository."),
])
async def test_single_failures_warn_and_continue(company, failure, message):
    handler = FakeGitHub({ISSUES: failure})
    signals, ctx = await run(company, handler)
    assert ctx.warnings == [message]
    assert set(by_id(signals)) == FORK_IDS  # forks still collected and enriched
    assert "/users/dana-ops" in handler.paths


@pytest.mark.parametrize("exc, text", [
    (httpx.ReadTimeout("timed out"), "GitHub: issues of acme/limo-sdk timed out"),
    (httpx.ConnectError("connection refused"), "GitHub: issues of acme/limo-sdk failed (ConnectError)"),
])
async def test_transport_errors_warn_and_continue(company, exc, text):
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    signals, ctx = await run(company, FakeGitHub({ISSUES: boom}))
    assert ctx.warnings == [text]
    assert set(by_id(signals)) == FORK_IDS


async def test_three_failures_in_a_row_stop_the_scan(company):
    company = configure(company, github_repos=[REPO, "rival/a", "rival/b"])

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    handler = FakeGitHub({p: down for p in (ISSUES, FORKS, "/repos/rival/a/issues", "/repos/rival/a/forks")})
    signals, ctx = await run(company, handler)
    assert signals == [] and len(handler.requests) == 3
    assert ctx.warnings[-1] == "GitHub: 3 failed requests in a row; stopped this scan"


async def test_missing_repo_warns_and_skips_its_forks(company):
    company = configure(company, github_repos=["gone/repo", REPO])
    handler = FakeGitHub({"/repos/gone/repo/issues": error("error_404.json", 404)})
    signals, ctx = await run(company, handler)
    assert "/repos/gone/repo/forks" not in handler.paths
    assert ctx.warnings == ["GitHub: repository gone/repo not found (HTTP 404); check the name in your signal settings"]
    assert set(by_id(signals)) == ANON_IDS


async def test_issues_disabled_still_watches_forks(company):
    handler = FakeGitHub({ISSUES: error("error_410_issues_disabled.json", 410)})
    signals, ctx = await run(company, handler)
    assert ctx.warnings == ["GitHub: issues are disabled on acme/limo-sdk, so it has no issue authors to watch"]
    assert set(by_id(signals)) == FORK_IDS


async def test_rejected_token_stops_with_clear_warning(company, token):
    handler = FakeGitHub({ISSUES: error("error_401_bad_credentials.json", 401)})
    signals, ctx = await run(company, handler)
    assert signals == [] and handler.paths == [ISSUES]
    assert ctx.warnings == ["GitHub: GITHUB_TOKEN was rejected (HTTP 401); fix or remove it. Stopped this scan"]


async def test_profile_failure_falls_back_to_login_only_lead(company):
    handler = FakeGitHub({"/users/dana-ops": httpx.Response(500, text="oops")})
    signals, ctx = await run(company, handler)
    assert ctx.warnings == ["GitHub: HTTP 500 for profile of dana-ops"]
    dana = by_id(signals)["github:issue:3456700212"].lead
    assert (dana.full_name, dana.github_username, dana.lead_company) == ("dana-ops", "dana-ops", "")
    assert by_id(signals)["github:issue:3456700210"].lead.full_name == "Kofi Mensah"
    assert handler.user_lookups().count("dana-ops") == 1  # a failed lookup is not retried in the same scan


async def test_bad_items_are_skipped_not_fatal(company):
    good = fixture("issues_acme_limo-sdk.json")[0]
    junk: list[Any] = [
        "not an object",
        None,
        {"number": 5, "title": "no user", "created_at": "2026-10-01T00:00:00Z"},
        {**good, "id": 1, "user": ["not", "a", "user"]},
        {**good, "id": 2, "created_at": "not a date"},
        {**good, "id": 3, "number": None},
        {**good, "id": 4, "labels": "bug", "title": None, "body": None},
    ]
    forks: list[Any] = [{"id": 7, "owner": None}, {"id": None, "owner": simple_user("x", 1)}, 42]
    handler = FakeGitHub({ISSUES: gh_response(200, junk + [good]), FORKS: gh_response(200, forks)})
    signals, ctx = await run(company, handler)
    ids = set(by_id(signals))
    assert ids == {"github:issue:3456700212", "github:issue:4"}
    untitled = by_id(signals)["github:issue:4"].signal
    assert untitled.title == "Opened an issue on acme/limo-sdk: #212" and untitled.summary == "#212"
    assert ctx.warnings == []


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("is_pr, groups, expected", [
    (False, {}, 50),
    (True, {}, 45),
    (False, {"integration": ["in production"]}, 70),
    (True, {"frustration": ["frustrated"]}, 75),
    (False, {"evaluation": ["evaluating"]}, 80),
    (False, {"evaluation": ["alternative to"], "frustration": ["unmaintained"]}, 85),
    (False, {"evaluation": ["x"], "frustration": ["y"], "integration": ["z"]}, 85),
])
def test_issue_strength_rules(is_pr, groups, expected):
    assert gh.issue_strength(is_pr, groups) == expected


def test_intent_groups_and_issue_text():
    text = gh.issue_text("<!-- evaluating? -->\n## Context\nWe're frustrated with the SDK in production. "
                         "![screenshot](https://x/y.png) Looking for an alternative to it.")
    assert text == "Context We're frustrated with the SDK in production. Looking for an alternative to it."
    groups = gh.intent_groups(text)
    assert groups == {"evaluation": ["alternative to", "looking for"], "frustration": ["frustrated"],
                      "integration": ["in production"]}
    assert gh.issue_text("<!-- unterminated template comment") == ""


@pytest.mark.parametrize("value, expected", [
    ("@northwind-logistics", "northwind-logistics"),
    ("@acmebank @fintech-guild", "acmebank"),
    ("  Globex Travel GmbH ", "Globex Travel GmbH"),
    (None, ""),
    ("", ""),
])
def test_clean_company(value, expected):
    assert gh.clean_company(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("northwind.example", "https://northwind.example"),
    ("http://mo.dev", "http://mo.dev"),
    ("HTTPS://Example.com/x", "HTTPS://Example.com/x"),
    ("", ""),
    (None, ""),
    ("my blog", ""),
])
def test_normalize_website(value, expected):
    assert gh.normalize_website(value) == expected


@pytest.mark.parametrize("profile, expected", [
    ({"blog": "https://www.globex-travel.example/", "email": None}, "globex-travel.example"),
    ({"blog": "globex.github.io", "email": "team@globex.example"}, "globex.example"),
    ({"blog": "", "email": "founder@gmail.com"}, ""),
    ({"blog": "https://medium.com/@globex", "email": None}, ""),
])
def test_company_domain(profile, expected):
    assert gh.company_domain(profile) == expected


def test_lead_ignores_noreply_email():
    lead = gh.github_lead(simple_user("pat", 1), {"login": "pat", "email": "1+pat@users.noreply.github.com",
                                                  "twitter_username": "@pat_x", "html_url": "https://github.com/pat"})
    assert lead.email == "" and lead.twitter == "pat_x" and lead.full_name == "pat"


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------


async def test_ingest_creates_scored_leads(company, token):
    signals, _ = await run(company, FakeGitHub())
    stats = ingest(company.id, signals)
    assert stats.errors == []
    assert stats.signals_new == len(signals) == len(TOKEN_IDS)

    people, total = repo.list_leads(company.id, kind="person", source="github")
    assert total == 5  # dana, kofi, lina, sam, mo: one lead per GitHub login
    dana = next(lead for lead in people if lead.github_username == "dana-ops")
    assert dana.full_name == "Dana Haddad" and dana.lead_company == "northwind-logistics"
    assert dana.location == "Dubai, United Arab Emirates" and dana.email == "dana@northwind.example"
    assert dana.intent_score > 0 and dana.score > 0 and dana.score_reasons
    dana_signals, _ = repo.list_signals(company.id, lead_id=dana.id)
    assert {s.external_id for s in dana_signals} == {"github:issue:3456700212", "github:fork:912340001"}
    lina = next(lead for lead in people if lead.github_username == "lina-k")
    lina_signals, _ = repo.list_signals(company.id, lead_id=lina.id)
    assert {s.type for s in lina_signals} == {"competitor_engagement", "github_star"}

    accounts, _ = repo.list_leads(company.id, kind="account")
    globex = next(lead for lead in accounts if lead.lead_company == "Globex Travel")
    assert globex.company_domain == "globex-travel.example" and globex.source == "github"
    assert globex.intent_score > 0

    gh_signals, total = repo.list_signals(company.id, source="github")
    assert total == len(TOKEN_IDS)
    assert {s.type for s in gh_signals} == {"competitor_engagement", "github_star"}

    again = ingest(company.id, (await run(company, FakeGitHub()))[0])
    assert again.signals_new == 0 and again.signals_duplicate == len(TOKEN_IDS)
    assert again.leads_new == 0  # the same people merge across scans by GitHub login


async def test_forks_404_when_only_stars_are_enabled(company, token):
    company = configure(company, enabled_types=["github_star"])
    handler = FakeGitHub({FORKS: error("error_404.json", 404)})
    signals, ctx = await run(company, handler)
    assert handler.paths == [FORKS]  # no stargazers or profile requests for a missing repo
    assert signals == []
    assert ctx.warnings == [f"GitHub: repository {REPO} not found (HTTP 404); check the name in your signal settings"]


async def test_run_scan_uses_the_collector(company, token, monkeypatch):
    from openberry import services

    monkeypatch.setattr(repo, "utcnow", lambda: datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(GitHubCollector, "request_interval", 0)
    handler = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stats = await services.run_scan(company.id, sources=["github"], client=client)
    assert stats["status"] == "ok"
    assert stats["collectors"]["github"] == {"found": len(TOKEN_IDS), "warnings": []}
    assert stats["signals_new"] == len(TOKEN_IDS) and stats["leads_new"] == 6  # 5 people + 1 organisation
    assert handler.requests[0].url.params["since"] == "2026-09-24T12:00:00Z"  # lookback_days=14


# --------------------------------------------------------------------------------------
# Review fixes
# --------------------------------------------------------------------------------------

# Documented payload shapes (API research: GET /users/{username}, star+json stargazers, simple-user).
SIMPLE_USER_KEYS = {
    "login", "id", "node_id", "avatar_url", "gravatar_id", "url", "html_url", "followers_url", "following_url",
    "gists_url", "starred_url", "subscriptions_url", "organizations_url", "repos_url", "events_url",
    "received_events_url", "type", "user_view_type", "site_admin",
}
PUBLIC_USER_KEYS = SIMPLE_USER_KEYS | {
    "name", "company", "blog", "location", "email", "hireable", "bio", "twitter_username", "public_repos",
    "public_gists", "followers", "following", "created_at", "updated_at",
}


def test_fixtures_match_the_documented_github_payloads():
    for path in FIXTURES.glob("user_*.json"):
        profile = json.loads(path.read_text())
        assert set(profile) == PUBLIC_USER_KEYS, path.name
        assert isinstance(profile["blog"], str)  # "" when unset, never null
    for page in ("stargazers_page1.json", "stargazers_page2.json", "stargazers_page3.json"):
        for entry in fixture(page):
            assert set(entry) == {"starred_at", "user"} and set(entry["user"]) == SIMPLE_USER_KEYS
    for item in fixture("issues_acme_limo-sdk.json"):
        assert set(item["user"]) == SIMPLE_USER_KEYS
        assert {"id", "number", "title", "body", "html_url", "created_at", "author_association", "labels"} <= set(item)
        if "pull_request" in item:
            assert {"url", "html_url", "diff_url", "patch_url", "merged_at"} <= set(item["pull_request"])
    for fork in fixture("forks_acme_limo-sdk.json"):
        assert set(fork["owner"]) == SIMPLE_USER_KEYS
        assert {"id", "full_name", "html_url", "created_at", "pushed_at", "description", "fork"} <= set(fork)


@pytest.mark.parametrize("title, body, is_pr", [
    ("Integration tests fail on Windows", "", False),
    ("String comparison is case sensitive", "Comparing 'A' and 'a' returns False.", False),
    ("Lazy evaluation of settings", "", False),
    ("Migrate to Pydantic v2", "Switching to the v2 validators.", True),
    ("Deployment docs are outdated", "", False),
])
def test_everyday_developer_wording_is_not_buying_intent(title, body, is_pr):
    groups = gh.intent_groups(f"{title}\n{body}")
    assert groups == {}
    assert gh.issue_strength(is_pr, groups) == (45 if is_pr else 50)


@pytest.mark.parametrize("text, group", [
    ("Is there an integration with Salesforce?", "integration"),
    ("We are migrating from Blacklane next month", "evaluation"),
    ("Running a pilot with our travel desk", "evaluation"),
])
def test_buying_intent_still_detected(text, group):
    assert group in gh.intent_groups(text)


async def test_placeholder_accounts_are_not_leads(company):
    issue = fixture("issues_acme_limo-sdk.json")[0]
    ghost = {**issue, "id": 1, "user": simple_user("ghost", 10137)}  # every deleted account becomes "ghost"
    mannequin = {**issue, "id": 2, "author_association": "MANNEQUIN",
                 "user": simple_user("imported-bob", 3, kind="Mannequin")}
    ghost_fork = make_fork("ghost", 10137, 990000001)
    handler = FakeGitHub({ISSUES: gh_response(200, [ghost, mannequin]), FORKS: gh_response(200, [ghost_fork])})
    signals, ctx = await run(company, handler)
    assert signals == [] and ctx.warnings == []
    assert handler.user_lookups() == []


async def test_html_403_on_stargazers_stops_instead_of_blaming_the_restriction(company, token):
    blocked = httpx.Response(403, text=(FIXTURES / "blocked_403.html").read_text(),
                             headers={"Content-Type": "text/html; charset=utf-8"})
    handler = FakeGitHub({STARS: blocked})
    signals, ctx = await run(company, handler)
    assert handler.paths == [ISSUES, FORKS, STARS]  # no further requests after the block page
    assert len(ctx.warnings) == 1 and "access refused (HTTP 403" in ctx.warnings[0]
    assert not any("administer" in w for w in ctx.warnings)
    assert set(by_id(signals)) == ANON_IDS  # what was already found is kept (not enriched)


async def test_insiders_forks_and_stars_are_not_leads(company, token):
    # rahul-acme is a MEMBER (he opened PR #209); "acme" owns the repo. Both fork it, rahul also stars it.
    forks = fixture("forks_acme_limo-sdk.json") + [make_fork("rahul-acme", 50999999, 990000101),
                                                    make_fork("Acme", 1, 990000102)]
    stars = fixture("stargazers_page3.json") + [{"starred_at": "2026-10-05T10:00:00Z",
                                                 "user": simple_user("rahul-acme", 50999999)}]
    handler = FakeGitHub({FORKS: gh_response(200, forks), (STARS, "3"): gh_response(200, stars)})
    signals, _ = await run(company, handler)
    assert set(by_id(signals)) == TOKEN_IDS
    assert "rahul-acme" not in handler.user_lookups() and "Acme" not in handler.user_lookups()


async def test_insider_seen_on_a_later_repo_is_dropped_from_earlier_forks(company):
    # rival/b lists dev-x as a COLLABORATOR; dev-x's fork of acme/limo-sdk (scanned first) is dropped too.
    issue = {**fixture("issues_acme_limo-sdk.json")[0], "id": 77, "author_association": "COLLABORATOR",
             "user": simple_user("dev-x", 4242)}
    forks = fixture("forks_acme_limo-sdk.json") + [make_fork("dev-x", 4242, 990000201)]
    handler = FakeGitHub({FORKS: gh_response(200, forks), "/repos/rival/b/issues": gh_response(200, [issue])})
    signals, _ = await run(configure(company, github_repos=[REPO, "rival/b"]), handler)
    assert "github:fork:990000201" not in by_id(signals)
    assert set(by_id(signals)) == ANON_IDS


def test_linkedin_profile_in_blog_becomes_linkedin_url():
    lead = gh.github_lead(simple_user("dana-ops", 1), {**fixture("user_dana-ops.json"),
                                                         "blog": "www.linkedin.com/in/dana-haddad/"})
    assert lead.linkedin_url == "https://www.linkedin.com/in/dana-haddad/"
    assert lead.website == ""
    company_page = gh.github_lead(simple_user("x", 2), {"blog": "https://linkedin.com/company/northwind"})
    assert company_page.linkedin_url == "" and company_page.website == "https://linkedin.com/company/northwind"


async def test_github_lead_merges_with_an_existing_linkedin_lead(company):
    from openberry.models import LeadIn

    existing, _ = repo.upsert_lead(company.id, LeadIn(full_name="Dana Haddad", title="Travel Manager",
                                                      linkedin_url="https://www.linkedin.com/in/dana-haddad"))
    profile = {**fixture("user_dana-ops.json"), "blog": "https://www.linkedin.com/in/dana-haddad/",
               "email": None, "twitter_username": None}
    handler = FakeGitHub({"/users/dana-ops": gh_response(200, profile)})
    signals, _ = await run(company, handler)
    stats = ingest(company.id, signals)
    assert stats.errors == []
    dana = repo.get_lead(existing.id)
    assert dana.github_username == "dana-ops" and dana.title == "Travel Manager"
    dana_signals, _ = repo.list_signals(company.id, lead_id=existing.id)
    assert {s.external_id for s in dana_signals} == {"github:issue:3456700212", "github:fork:912340001"}
