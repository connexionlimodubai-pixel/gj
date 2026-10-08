"""Reddit collector: OAuth app-only token + search, mapped to signals. All HTTP is mocked."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from openberry import __version__, repo
from openberry.collectors import COLLECTORS, CollectContext
from openberry.collectors.reddit import MAX_REQUESTS, RedditCollector, user_agent
from openberry.config import get_settings
from openberry.models import Company
from openberry.services import ingest

FIXTURES = Path(__file__).parent / "fixtures" / "reddit"
NOW = datetime.now(timezone.utc)
TOKEN = json.loads((FIXTURES / "token.json").read_text())["access_token"]
UA = f"python:openberry:{__version__} (+https://github.com/connexionlimodubai-pixel/gj)"
SEARCH_PARAMS = {"sort": "new", "t": "month", "type": "link", "limit": "50", "raw_json": "1"}
RATE_HEADERS = {"x-ratelimit-used": "4", "x-ratelimit-remaining": "96.0", "x-ratelimit-reset": "412"}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def listing(name: str) -> dict[str, Any]:
    """Load a Listing fixture and shift its times so the newest post is one hour old."""
    data = json.loads((FIXTURES / name).read_text())
    children = data["data"]["children"]
    shift = (NOW.timestamp() - 3600) - max(c["data"]["created_utc"] for c in children)
    for child in children:
        child["data"]["created_utc"] += shift
    return data


def created(name: str, post_id: str) -> datetime:
    post = next(c["data"] for c in listing(name)["data"]["children"] if c["data"]["id"] == post_id)
    return datetime.fromtimestamp(post["created_utc"], tz=timezone.utc)


def ok(name: str = "search_all.json", **headers: str) -> httpx.Response:
    return httpx.Response(200, json=listing(name), headers={**RATE_HEADERS, **headers})


def make_post(post_id: str, hours_ago: float, title: str = "Need a chauffeur next week") -> dict[str, Any]:
    return {"kind": "t3", "data": {
        "id": post_id, "name": f"t3_{post_id}", "title": title, "selftext": "", "author": f"user_{post_id}",
        "subreddit": "dubai", "permalink": f"/r/dubai/comments/{post_id}/x/", "is_self": True,
        "num_comments": 0, "score": 1, "created_utc": (NOW - timedelta(hours=hours_ago)).timestamp()}}


def page(children: list[dict[str, Any]], after: str | None) -> httpx.Response:
    return httpx.Response(200, json={"kind": "Listing", "data": {"after": after, "before": None,
                                                                 "children": children}}, headers=RATE_HEADERS)


class FakeReddit:
    """MockTransport handler that records requests and routes token vs search calls."""

    def __init__(self, search: Callable[[httpx.Request], httpx.Response] | None = None,
                 token: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.search = search or (lambda request: ok())
        self.token = token or (lambda request: httpx.Response(200, json={"access_token": TOKEN, "token_type": "bearer",
                                                                          "expires_in": 86400, "scope": "*"}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "www.reddit.com" and request.url.path == "/api/v1/access_token":
            return self.token(request)
        if request.url.host == "oauth.reddit.com":
            return self.search(request)
        return httpx.Response(404, text="unexpected host")

    @property
    def token_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "www.reddit.com"]

    @property
    def searches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "oauth.reddit.com"]


async def run(company: Company, fake: FakeReddit, *, since: datetime | None = None, max_items: int = 200,
              collector: RedditCollector | None = None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        ctx = CollectContext(client=client, since=since or NOW - timedelta(days=14), settings=get_settings(),
                             max_items=max_items)
        out = await (collector or RedditCollector()).collect(company, ctx)
    return out, ctx


def site_wide(company: Company, **signals: Any) -> Company:
    """The conftest company without subreddits (searches all of Reddit), with optional overrides."""
    return company.model_copy(update={"signals": company.signals.model_copy(update={"subreddits": [], **signals})})


def with_signals(company: Company, **signals: Any) -> Company:
    return company.model_copy(update={"signals": company.signals.model_copy(update=signals)})


@pytest.fixture(autouse=True)
def reddit_creds(settings):
    settings.reddit_client_id = "cid-123"
    settings.reddit_client_secret = "s3cret"
    return settings


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def test_registered_and_is_configured(company, settings):
    collector = RedditCollector()
    assert isinstance(COLLECTORS["reddit"], RedditCollector)
    assert collector.signal_types == ("competitor_engagement", "keyword_mention")
    assert collector.is_configured(company)
    assert collector.is_configured(company.model_copy(update={"competitors": []}))       # keywords only
    assert collector.is_configured(with_signals(company, keywords=[]))                      # competitors only
    assert not collector.is_configured(with_signals(company.model_copy(update={"competitors": []}), keywords=[]))
    settings.reddit_client_secret = ""
    assert not collector.is_configured(company)
    settings.reddit_client_secret, settings.reddit_client_id = "s3cret", "  "
    assert not collector.is_configured(company)
    assert not collector.enabled_for(company)


async def test_missing_credentials_warns_without_requests(company, settings):
    settings.reddit_client_id = ""
    fake = FakeReddit()
    out, ctx = await run(company, fake)
    assert out == [] and fake.requests == []
    assert ctx.warnings == ["Reddit: REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set; skipped."]


def test_user_agent_format(settings, monkeypatch):
    assert user_agent(settings) == UA
    monkeypatch.setattr(settings, "reddit_username", "u/openberry_ops", raising=False)
    assert user_agent(settings) == f"python:openberry:{__version__} (by /u/openberry_ops; " \
                                   "+https://github.com/connexionlimodubai-pixel/gj)"


# --------------------------------------------------------------------------------------
# Requests and mapping
# --------------------------------------------------------------------------------------

async def test_token_request_and_site_wide_searches(company):
    fake = FakeReddit()
    out, ctx = await run(site_wide(company), fake)

    (tok,) = fake.token_requests
    assert tok.method == "POST" and str(tok.url) == "https://www.reddit.com/api/v1/access_token"
    assert tok.headers["authorization"] == "Basic " + base64.b64encode(b"cid-123:s3cret").decode()
    assert tok.headers["user-agent"] == UA
    assert tok.headers["content-type"] == "application/x-www-form-urlencoded"
    assert tok.content == b"grant_type=client_credentials"

    # Competitors first, then keywords; one query per term site-wide, multi-word terms quoted.
    assert [r.url.params["q"] for r in fake.searches] == ["Blacklane", '"Careem Business"', "chauffeur",
                                                         '"corporate travel"']
    for request, q in zip(fake.searches, ["Blacklane", '"Careem Business"', "chauffeur", '"corporate travel"']):
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "oauth.reddit.com"
        assert request.url.path == "/search"
        assert dict(request.url.params) == {"q": q, **SEARCH_PARAMS}
        assert request.headers["authorization"] == f"bearer {TOKEN}"
        assert request.headers["user-agent"] == UA

    # Every search returned the same listing: posts are de-duplicated, skipped authors/NSFW/old dropped.
    assert [r.signal.external_id for r in out] == ["reddit:1o1aaa1", "reddit:1o1aaa4", "reddit:1o1aaa5"]
    assert ctx.warnings == []


async def test_post_mapping_and_strength_rules(company):
    out, _ = await run(site_wide(company), FakeReddit())
    by_id = {r.signal.external_id: r for r in out}

    churn = by_id["reddit:1o1aaa1"]
    sig = churn.signal
    assert sig.type == "competitor_engagement" and sig.source == "reddit"
    assert sig.strength == 85  # churn phrase next to a competitor
    assert sig.title == "r/dubai: Looking for a Blacklane alternative for our exec team"
    assert sig.url == "https://www.reddit.com/r/dubai/comments/1o1aaa1/looking_for_a_blacklane_alternative_for_our_exec/"
    assert sig.summary.startswith("We book ~40 airport transfers") and len(sig.summary) <= 500
    assert sig.occurred_at == created("search_all.json", "1o1aaa1") and sig.occurred_at.tzinfo is not None
    assert sig.raw == {
        "post_id": "t3_1o1aaa1", "subreddit": "dubai", "author": "ops_lead_jane", "score": 14, "num_comments": 9,
        "matched_terms": ["Blacklane", "chauffeur"], "matched_in_text": True, "intent_phrase": "alternative for",
        "query": "Blacklane", "flair": "Ask Dubai", "link_url": "",
    }
    assert churn.lead is not None and churn.account == ""
    assert churn.lead.full_name == "ops_lead_jane"
    assert churn.lead.profile_url == "https://www.reddit.com/user/ops_lead_jane"
    assert churn.lead.source == "reddit"

    comparison = by_id["reddit:1o1aaa4"].signal  # "vs" = buying intent 75, +5 for 15 comments
    assert (comparison.type, comparison.strength, comparison.raw["intent_phrase"]) == ("competitor_engagement", 80, "vs")

    link_post = by_id["reddit:1o1aaa5"].signal  # plain mention in a link post
    assert link_post.strength == 50
    assert link_post.summary == "https://i.redd.it/k2v9x0abc.jpeg"
    assert link_post.raw["link_url"] == "https://i.redd.it/k2v9x0abc.jpeg"


async def test_subreddit_search_or_query_and_weak_match(company):
    fake = FakeReddit(search=lambda request: ok("search_dubai.json"))
    out, ctx = await run(company, fake)  # conftest company watches r/dubai

    (search,) = fake.searches
    assert search.url.path == "/r/dubai/search"
    assert dict(search.url.params) == {"q": 'Blacklane OR "Careem Business" OR chauffeur OR "corporate travel"',
                                       **SEARCH_PARAMS, "restrict_sr": "1"}
    got = {r.signal.external_id: (r.signal.type, r.signal.strength, r.signal.raw["matched_terms"]) for r in out}
    assert got == {
        # switching from + competitor = 85, +5 busy thread
        "reddit:1o2bbb1": ("competitor_engagement", 90, ["Careem Business", "corporate travel"]),
        # plural keyword match, question title
        "reddit:1o2bbb2": ("keyword_mention", 60, ["chauffeur"]),
        # Reddit matched it but no term is visible: weak 35, +5 busy thread
        "reddit:1o2bbb3": ("keyword_mention", 40, []),
    }
    assert not out[2].signal.raw["matched_in_text"]
    assert ctx.warnings == []


async def test_since_filter_and_time_window(company):
    fake = FakeReddit()
    out, _ = await run(site_wide(company, keywords=[]), fake, since=NOW - timedelta(hours=12))
    assert fake.searches[0].url.params["t"] == "day"
    assert [r.signal.external_id for r in out] == ["reddit:1o1aaa1", "reddit:1o1aaa4"]
    assert all(r.signal.occurred_at > NOW - timedelta(hours=12) for r in out)

    fake = FakeReddit()
    await run(site_wide(company, keywords=[]), fake, since=NOW - timedelta(days=7))
    assert fake.searches[0].url.params["t"] == "week"
    fake = FakeReddit()
    out, _ = await run(site_wide(company, keywords=[]), fake, since=NOW - timedelta(days=60))
    assert fake.searches[0].url.params["t"] == "year"
    assert "reddit:1o1aaa6" in {r.signal.external_id for r in out}  # 20-day-old post is inside a 60-day lookback


async def test_stable_ids_across_scans(company):
    first, _ = await run(company, FakeReddit(search=lambda request: ok("search_dubai.json")))
    second, _ = await run(company, FakeReddit(search=lambda request: ok("search_dubai.json")))
    assert [r.signal.external_id for r in first] == [r.signal.external_id for r in second]
    assert ingest(company.id, first).signals_new == 3
    again = ingest(company.id, second)
    assert again.signals_new == 0 and again.signals_duplicate == 3 and again.leads_new == 0


# --------------------------------------------------------------------------------------
# Caps and paging
# --------------------------------------------------------------------------------------

async def test_many_terms_are_packed_under_the_request_cap(company):
    keywords = [f"topic{i}" for i in range(13)]
    fake = FakeReddit(search=lambda request: page([], None))
    _, ctx = await run(site_wide(company, keywords=keywords), fake)
    queries = [r.url.params["q"] for r in fake.searches]
    assert len(queries) == 8 <= MAX_REQUESTS  # 15 terms in pairs
    assert queries[0] == 'Blacklane OR "Careem Business"' and queries[-1] == "topic12"
    assert ctx.warnings == []


async def test_subreddit_request_cap_warns(company):
    subs = [f"sub{i}" for i in range(12)]
    fake = FakeReddit(search=lambda request: page([], None))
    _, ctx = await run(with_signals(company, subreddits=subs), fake)
    assert [r.url.path for r in fake.searches] == [f"/r/sub{i}/search" for i in range(MAX_REQUESTS)]
    assert ctx.warnings == ["Reddit: only the first 10 of 12 searches run per scan; list fewer subreddits to cover them all."]


async def test_invalid_subreddit_names_are_ignored(company):
    fake = FakeReddit(search=lambda request: page([], None))
    _, ctx = await run(with_signals(company, subreddits=["dubai", "../api/v1/me"]), fake)
    assert [r.url.path for r in fake.searches] == ["/r/dubai/search"]
    assert ctx.warnings == ["Reddit: ignored invalid subreddit name(s): ../api/v1/me."]


async def test_max_items_stops_collecting_and_requesting(company):
    fake = FakeReddit()
    out, _ = await run(site_wide(company), fake, max_items=2)
    assert [r.signal.external_id for r in out] == ["reddit:1o1aaa1", "reddit:1o1aaa4"]
    assert len(fake.searches) == 1


async def test_pages_while_full_and_inside_window(company):
    first = [make_post(f"p{i:02d}", hours_ago=1 + i) for i in range(50)]
    second = [make_post(f"q{i:02d}", hours_ago=60 + i) for i in range(5)]

    def search(request: httpx.Request) -> httpx.Response:
        return page(second, None) if request.url.params.get("after") else page(first, "t3_p49")

    fake = FakeReddit(search=search)
    out, _ = await run(site_wide(company.model_copy(update={"competitors": []}), keywords=["chauffeur"]), fake)
    assert [r.url.params.get("after") for r in fake.searches] == [None, "t3_p49"]
    assert len(out) == 55


async def test_no_paging_once_page_reaches_older_posts(company):
    posts = [make_post(f"p{i:02d}", hours_ago=1 + i * 10) for i in range(50)]  # oldest ~20 days ago
    fake = FakeReddit(search=lambda request: page(posts, "t3_p49"))
    out, _ = await run(site_wide(company.model_copy(update={"competitors": []}), keywords=["chauffeur"]), fake)
    assert len(fake.searches) == 1
    assert len(out) == 34  # posts within 14 days only


# --------------------------------------------------------------------------------------
# Errors: warnings, never exceptions
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("response, expected", [
    (lambda r: httpx.Response(401, json=json.loads((FIXTURES / "token_401.json").read_text())),
     "Reddit: access token request failed (HTTP 401: check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET); skipped this scan."),
    (lambda r: httpx.Response(403, text=(FIXTURES / "blocked_403.html").read_text(), headers={"content-type": "text/html"}),
     "Reddit: access token request failed (HTTP 403: blocked, or the app is not approved for Data API access); "
     "skipped this scan."),
    (lambda r: httpx.Response(200, text="<html>maintenance</html>"),
     "Reddit: token endpoint returned no access token (not JSON); skipped this scan."),
    (lambda r: httpx.Response(200, json={"error": "unsupported_grant_type"}),
     "Reddit: token endpoint returned no access token (unsupported_grant_type); skipped this scan."),
])
async def test_token_failure_is_one_warning(company, response, expected):
    fake = FakeReddit(token=response)
    out, ctx = await run(company, fake)
    assert out == [] and fake.searches == []
    assert ctx.warnings == [expected]


async def test_token_network_error(company):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    out, ctx = await run(company, FakeReddit(token=boom))
    assert out == []
    assert ctx.warnings == ["Reddit: could not reach the token endpoint (ConnectError); skipped this scan."]


async def test_token_is_cached_and_dropped_after_401(company):
    collector = RedditCollector()
    fake = FakeReddit(search=lambda request: ok("search_dubai.json"))
    await run(company, fake, collector=collector)
    await run(company, fake, collector=collector)
    assert len(fake.token_requests) == 1 and len(fake.searches) == 2

    rejected = FakeReddit(search=lambda request: httpx.Response(401, json={"message": "Unauthorized", "error": 401}))
    out, ctx = await run(company, rejected, collector=collector)
    assert out == [] and rejected.token_requests == [] and len(rejected.searches) == 1
    assert ctx.warnings == ["Reddit: access token rejected (HTTP 401); stopped. A new token is requested next scan."]
    fresh = FakeReddit(search=lambda request: ok("search_dubai.json"))
    await run(company, fresh, collector=collector)
    assert len(fresh.token_requests) == 1


async def test_blocked_403_html_stops_the_scan(company):
    blocked = (FIXTURES / "blocked_403.html").read_text()
    fake = FakeReddit(search=lambda request: httpx.Response(403, text=blocked, headers={"content-type": "text/html"}))
    out, ctx = await run(site_wide(company), fake)
    assert out == [] and len(fake.searches) == 1
    assert ctx.warnings == ["Reddit: request blocked (HTTP 403). Check that the app is approved for Data API access; "
                            "stopped for this scan."]


async def test_private_or_missing_subreddit_is_skipped(company):
    private = json.loads((FIXTURES / "private_403.json").read_text())

    def search(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/r/secretclub/search":
            return httpx.Response(403, json=private)
        if request.url.path == "/r/nosuchsub123/search":
            return httpx.Response(302, headers={"location": "https://oauth.reddit.com/subreddits/search?q=nosuchsub123"})
        return ok("search_dubai.json")

    fake = FakeReddit(search=search)
    out, ctx = await run(with_signals(company, subreddits=["secretclub", "nosuchsub123", "dubai"]), fake)
    assert [r.url.path for r in fake.searches] == ["/r/secretclub/search", "/r/nosuchsub123/search", "/r/dubai/search"]
    assert len(out) == 3
    assert ctx.warnings == ["Reddit: r/secretclub is unavailable (private); skipped it.",
                            "Reddit: r/nosuchsub123 is unavailable (HTTP 302); skipped it."]


async def test_429_stops_politely(company):
    fake = FakeReddit(search=lambda request: httpx.Response(429, text="Too Many Requests",
                                                            headers={"x-ratelimit-remaining": "0",
                                                                     "x-ratelimit-reset": "30"}))
    out, ctx = await run(site_wide(company), fake)
    assert out == [] and len(fake.searches) == 1
    assert ctx.warnings == ["Reddit: rate limited (HTTP 429, resets in 30s); stopped for this scan."]


async def test_rate_limit_headers_stop_after_current_page(company):
    fake = FakeReddit(search=lambda request: ok(**{"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "95"}))
    out, ctx = await run(site_wide(company), fake)
    assert len(fake.searches) == 1 and len(out) == 3  # this page is still used
    assert ctx.warnings == ["Reddit: API rate limit used up, resets in 95s; stopped for this scan."]


async def test_server_error_and_bad_json_are_skipped(company):
    responses = iter([
        httpx.Response(500, text="<html>internal error</html>"),
        httpx.Response(200, text="{not json"),
        ok(),  # a success resets the consecutive-failure counter
        httpx.Response(200, json={"kind": "Listing", "data": {"children": "oops"}}),
    ])
    fake = FakeReddit(search=lambda request: next(responses))
    out, ctx = await run(site_wide(company), fake)
    assert len(fake.searches) == 4 and len(out) == 3
    assert ctx.warnings == [
        "Reddit: search 'Blacklane' in all of Reddit failed (HTTP 500).",
        "Reddit: search '\"Careem Business\"' in all of Reddit returned an unexpected response; skipped it.",
        "Reddit: search '\"corporate travel\"' in all of Reddit returned an unexpected response; skipped it.",
    ]


async def test_timeouts_give_up_after_three_in_a_row(company):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    fake = FakeReddit(search=timeout)
    out, ctx = await run(site_wide(company), fake)
    assert out == [] and len(fake.searches) == 3
    assert ctx.warnings[-1] == "Reddit: 3 requests failed in a row; stopped for this scan."
    assert ctx.warnings[0] == "Reddit: search 'Blacklane' in all of Reddit failed (ReadTimeout)."


async def test_malformed_posts_are_skipped(company):
    good = make_post("good1", hours_ago=2)
    no_id = make_post("x", hours_ago=2)
    no_id["data"].pop("id")
    no_id["data"].pop("name")
    bad_time = make_post("bad2", hours_ago=2)
    bad_time["data"]["created_utc"] = "yesterday-ish"
    not_a_post = {"kind": "t5", "data": {"display_name": "chauffeurs"}}
    fake = FakeReddit(search=lambda request: page([no_id, bad_time, not_a_post, "junk", good], None))
    out, ctx = await run(site_wide(company.model_copy(update={"competitors": []}), keywords=["chauffeur"]), fake)
    assert [r.signal.external_id for r in out] == ["reddit:good1"]
    assert ctx.warnings == ["Reddit: skipped 2 malformed post(s)."]


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------

async def test_ingest_creates_scored_leads(company):
    site, _ = await run(site_wide(company), FakeReddit())
    subs, _ = await run(company, FakeReddit(search=lambda request: ok("search_dubai.json")))
    stats = ingest(company.id, site + subs)

    assert stats.errors == []
    assert stats.signals_new == 6
    # ops_lead_jane posted in both listings and merges into one lead by profile URL.
    assert stats.leads_new == 5 and stats.leads_updated == 1

    leads, total = repo.list_leads(company.id, limit=50)
    assert total == 5
    jane = next(lead for lead in leads if lead.full_name == "ops_lead_jane")
    assert jane.kind == "person" and jane.source == "reddit"
    assert jane.profile_url == "https://www.reddit.com/user/ops_lead_jane"
    signals, count = repo.list_signals(company.id, lead_id=jane.id)
    assert count == 2 and {s.external_id for s in signals} == {"reddit:1o1aaa1", "reddit:1o2bbb3"}
    assert all(s.source == "reddit" for s in signals)
    assert all(lead.intent_score > 0 and lead.score > 0 for lead in leads)
    sarah = next(lead for lead in leads if lead.full_name == "dxb_ea_sarah")
    weak = next(lead for lead in leads if lead.full_name == "limo_fan")
    assert sarah.intent_score > weak.intent_score  # strong churn signal outranks a plain mention
