"""Reddit collector: recent posts that mention your topics or competitors, via Reddit's official API.

How it works
    Application-only OAuth ("client credentials" grant), then Reddit search:

    1. ``POST https://www.reddit.com/api/v1/access_token`` with HTTP basic auth
       (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET) and ``grant_type=client_credentials``
       -> ``{"access_token": ..., "token_type": "bearer", "expires_in": 86400}``.
       The token is cached in memory until shortly before it expires.
    2. ``GET https://oauth.reddit.com/search`` (all of Reddit), or
       ``GET https://oauth.reddit.com/r/{subreddit}/search`` with ``restrict_sr=1`` when the
       company lists subreddits, with ``q``, ``sort=new``, ``t`` (day/week/month/year from the
       lookback), ``type=link``, ``limit=50``, ``raw_json=1``, ``Authorization: bearer <token>``
       and a Reddit-style User-Agent (``python:openberry:<version> (...)``).

    Site-wide, every competitor and keyword gets its own query (packed into OR-queries only when
    there are more terms than the request cap). Inside subreddits, all terms are OR-ed into one
    query per subreddit. Each post (``Listing.data.children[].data``, kind ``t3``) becomes one
    signal whose lead is the author, identified by ``https://www.reddit.com/user/<name>`` so the
    same person merges across scans. ``[deleted]``, AutoModerator and NSFW posts are skipped.

Terms of service -- read before enabling
    * Reddit has blocked unauthenticated ``.json`` access (HTTP 403 with an HTML page) since
      mid-2026, so this source only runs with the credentials of a Reddit app you registered at
      https://www.reddit.com/prefs/apps ("script" or "web app") and that Reddit approved under its
      Responsible Builder Policy. It is off by default.
    * Reddit's Data API Terms require a separate agreement with Reddit for commercial use, and
      lead generation is commercial use. Running this collector for a business without that
      agreement may breach Reddit's terms. That decision and its risk belong to the operator.
    * Reddit announced on 2026-09-30 that it accepts no new public API access requests after
      2026-10-31 and will close the remaining public Data API access in phases through March
      2027. Expect this source to stop working. Hacker News, job boards and SEC filings are the
      durable free sources.

Limits and politeness
    The free OAuth tier allows 100 queries a minute per client id, reported in the
    X-Ratelimit-Used / -Remaining / -Reset headers. Each scan sends at most one token request plus
    MAX_REQUESTS searches, one at a time. It stops when X-Ratelimit-Remaining reaches 0, on HTTP 429,
    on 401 or a blocking 403, or after MAX_CONSECUTIVE_FAILURES failed requests in a row. It only
    asks for a further page when a page is full and still inside the lookback window. Search covers
    posts, not comments.

Strength (SignalIn.strength, 50 = typical)
    35  search hit, but none of the terms is visible in the post (stemming, author or URL match)
    50  a keyword or competitor is mentioned
    60  ... in a post whose title is a question
    75  a buying-intent phrase ("looking for", "recommend", "alternative to", "anyone use", ...)
    85  a churn phrase next to a competitor mention ("switching from", "frustrated with", "cancel", ...)
    +5  busy thread (10 or more comments), capped at 90
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import __version__
from ..config import Settings, get_settings
from ..models import Company, LeadIn, SignalIn
from .base import CollectContext, Collector, RawSignal, parse_time, strip_html, truncate

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
WEB_BASE = "https://www.reddit.com"

MAX_REQUESTS = 10             # search requests per scan (plus at most one token request)
PAGE_SIZE = 50
MAX_QUERY_CHARS = 512         # Reddit's limit for `q`
MAX_CONSECUTIVE_FAILURES = 3
TOKEN_EXPIRY_MARGIN = 60      # seconds: renew the cached token a little before Reddit expires it

COMPETITOR, KEYWORD = "competitor", "keyword"
SKIP_AUTHORS = frozenset({"[deleted]", "[removed]", "automoderator"})
REMOVED_BODIES = frozenset({"[deleted]", "[removed]"})
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{1,20}$")

# Phrases that suggest someone is choosing a vendor (matched on lower-cased title + body).
INTENT_PATTERNS: tuple[str, ...] = (
    r"looking for", r"recommend(?:s|ed|ation|ations)?", r"any suggestions?", r"suggestions? for",
    r"alternatives? (?:to|for)", r"switch(?:ing)? (?:from|away from)", r"anyone (?:here )?(?:use|used|using|tried)",
    r"has anyone (?:tried|used)", r"frustrated with", r"fed up with", r"in the market for",
    r"(?:what|which|who) (?:\w+ )?(?:do|should|would) (?:you|i|we) (?:use|pick|choose|go with)",
    r"best \w+(?: \w+)? for", r"need (?:a|an) (?:good|reliable|new)? ?\w+", r"vs\.?", r"versus",
)
# Phrases that suggest someone is unhappy with, or leaving, a vendor. Count only next to a competitor.
CHURN_PATTERNS: tuple[str, ...] = (
    r"switch(?:ing|ed)? (?:from|away from)", r"moving (?:away )?from", r"alternatives? (?:to|for)",
    r"replac(?:e|ing|ement for)", r"frustrated with", r"fed up with", r"unhappy with",
    r"disappointed (?:with|by)", r"cancel(?:l?ing|l?ed)?", r"(?:terrible|awful|horrible|bad) (?:experience|service)",
    r"stop(?:ped)? using",
)
_INTENT_RE = [re.compile(r"(?<![a-z0-9])" + p + r"(?![a-z0-9])") for p in INTENT_PATTERNS]
_CHURN_RE = [re.compile(r"(?<![a-z0-9])" + p + r"(?![a-z0-9])") for p in CHURN_PATTERNS]


# --------------------------------------------------------------------------------------
# Query planning
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Term:
    text: str
    kind: str  # COMPETITOR | KEYWORD


@dataclass(frozen=True)
class _Search:
    scope: str                    # "" = all of Reddit, else a subreddit name
    terms: tuple[_Term, ...]
    after: str = ""               # pagination cursor (fullname of the last post seen)

    @property
    def query(self) -> str:
        return " OR ".join(_quote(t.text) for t in self.terms)

    @property
    def label(self) -> str:
        where = f"r/{self.scope}" if self.scope else "all of Reddit"
        return f"'{truncate(self.query, 60)}' in {where}"


def _quote(term: str) -> str:
    return f'"{term}"' if " " in term else term


def _company_terms(company: Company) -> list[_Term]:
    """Competitors first (stronger signal type), then keywords; case-insensitive dedupe."""
    seen: set[str] = set()
    terms: list[_Term] = []
    for kind, values in ((COMPETITOR, company.competitors), (KEYWORD, company.signals.keywords)):
        for value in values:
            text = re.sub(r"\s+", " ", str(value).replace('"', " ")).strip()[:100]
            if text and text.lower() not in seen:
                seen.add(text.lower())
                terms.append(_Term(text, kind))
    return terms


def _pack(terms: list[_Term], groups: int = 1) -> list[tuple[_Term, ...]]:
    """Split terms into about `groups` OR-queries, each within Reddit's query length limit."""
    size = max(1, -(-len(terms) // max(1, groups)))  # ceil division
    out: list[tuple[_Term, ...]] = []
    for start in range(0, len(terms), size):
        current: list[_Term] = []
        for term in terms[start:start + size]:
            if current and len(_Search("", (*current, term)).query) > MAX_QUERY_CHARS:
                out.append(tuple(current))
                current = []
            current.append(term)
        if current:
            out.append(tuple(current))
    return out


def _plan(terms: list[_Term], subreddits: list[str]) -> list[_Search]:
    if subreddits:
        groups = _pack(terms)
        return [_Search(sub, group) for sub in subreddits for group in groups]
    if len(terms) <= MAX_REQUESTS:
        return [_Search("", (term,)) for term in terms]
    return [_Search("", group) for group in _pack(terms, MAX_REQUESTS)]


def _time_filter(since: datetime) -> str:
    """Smallest Reddit `t` window that covers the lookback (an hour of slack for scan latency)."""
    age = datetime.now(timezone.utc) - since
    for name, days in (("day", 1), ("week", 7), ("month", 31), ("year", 365)):
        if age <= timedelta(days=days, hours=1):
            return name
    return "all"


def user_agent(settings: Settings) -> str:
    """Reddit wants '<platform>:<app id>:<version> (by /u/<user>)' and never a spoofed browser UA."""
    # `reddit_username` is optional and only used when a Settings field for it exists.
    username = str(getattr(settings, "reddit_username", "") or "").strip().removeprefix("/").removeprefix("u/")
    url = re.search(r"https?://[^\s);]+", settings.user_agent or "")
    details = "; ".join(p for p in (f"by /u/{username}" if username else "", f"+{url.group(0)}" if url else "") if p)
    return f"python:openberry:{__version__}" + (f" ({details})" if details else "")


def _has_credentials(settings: Settings) -> bool:
    return bool(settings.reddit_client_id.strip() and settings.reddit_client_secret.strip())


# --------------------------------------------------------------------------------------
# Post -> signal
# --------------------------------------------------------------------------------------

def _mentions(text: str, terms: list[str]) -> list[str]:
    """Whole-word, case-insensitive matches that also accept plurals/possessives ('chauffeurs')."""
    hay = text.lower()
    hits = []
    for term in terms:
        pattern = r"\s+".join(re.escape(w) for w in term.lower().split())
        if pattern and re.search(r"(?<![a-z0-9])" + pattern + r"(?:'s|s|es)?(?![a-z0-9])", hay):
            hits.append(term)
    return hits


def _first_phrase(patterns: list[re.Pattern[str]], text: str) -> str:
    for pattern in patterns:
        if m := pattern.search(text):
            return m.group(0)
    return ""


def _strength(title: str, text: str, competitor_hit: bool, visible: bool, comments: int) -> tuple[int, str]:
    """Return (strength, intent phrase found) following the rule in the module docstring."""
    hay = text.lower()
    phrase = ""
    if not visible:
        value = 35
    elif competitor_hit and (phrase := _first_phrase(_CHURN_RE, hay)):
        value = 85
    elif phrase := _first_phrase(_INTENT_RE, hay):
        value = 75
    elif title.rstrip().endswith("?"):
        value = 60
    else:
        value = 50
    if comments >= 10:
        value = min(90, value + 5)
    return value, phrase


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _post_signal(post: dict[str, Any], terms: list[_Term], search: _Search, since: datetime) -> RawSignal | None:
    """Map one t3 post to a RawSignal, or None when it should be skipped. Raises on malformed data."""
    author = str(post.get("author") or "").strip()
    if not author or author.lower() in SKIP_AUTHORS or post.get("over_18") or not _USERNAME_RE.match(author):
        return None
    post_id = str(post.get("id") or "").strip() or str(post.get("name") or "").removeprefix("t3_")
    if not post_id:
        raise ValueError("post without id")
    occurred = parse_time(post.get("created_utc"))
    if occurred is None:
        raise ValueError(f"post {post_id} has no valid created_utc")
    if occurred <= since:
        return None

    title = strip_html(post.get("title"))
    body = strip_html(post.get("selftext"))
    if body in REMOVED_BODIES:
        body = ""
    link = "" if post.get("is_self", True) else str(post.get("url") or "")
    subreddit = str(post.get("subreddit") or search.scope or "").strip()
    text = f"{title}\n{body}\n{link}"

    competitors = _mentions(text, [t.text for t in terms if t.kind == COMPETITOR])
    keywords = _mentions(text, [t.text for t in terms if t.kind == KEYWORD])
    visible = bool(competitors or keywords)
    if visible:
        is_competitor = bool(competitors)
    else:  # Reddit matched it for a reason we cannot see; attribute it to the query, weakly.
        is_competitor = all(t.kind == COMPETITOR for t in search.terms)
    comments = _int(post.get("num_comments"))
    strength, phrase = _strength(title, f"{title}\n{body}", bool(competitors), visible, comments)

    permalink = str(post.get("permalink") or f"/comments/{post_id}/")
    signal = SignalIn(
        type="competitor_engagement" if is_competitor else "keyword_mention",
        title=truncate(f"r/{subreddit}: {title}" if title else f"Reddit post by u/{author}", 160),
        summary=truncate(body or link or title, 500),
        url=WEB_BASE + permalink if permalink.startswith("/") else permalink,
        source="reddit",
        external_id=f"reddit:{post_id}",
        strength=strength,
        occurred_at=occurred,
        raw={
            "post_id": f"t3_{post_id}",
            "subreddit": subreddit,
            "author": author,
            "score": _int(post.get("score")),
            "num_comments": comments,
            "matched_terms": competitors + keywords,
            "matched_in_text": visible,
            "intent_phrase": phrase,
            "query": search.query,
            "flair": post.get("link_flair_text") or "",
            "link_url": link,
        },
    )
    lead = LeadIn(full_name=author, profile_url=f"{WEB_BASE}/user/{author}", source="reddit")
    return RawSignal(signal=signal, lead=lead)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------

OK, FAILED, SKIP_SCOPE, STOP = "ok", "failed", "skip_scope", "stop"


@dataclass
class _Page:
    outcome: str
    posts: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0                # children on the page before filtering (to decide on paging)
    after: str = ""
    stop_after: bool = False      # rate-limit budget spent: use this page, then stop


def _reset_hint(resp: httpx.Response) -> str:
    raw = resp.headers.get("x-ratelimit-reset") or resp.headers.get("retry-after")
    try:
        return f", resets in {int(float(raw))}s" if raw else ""
    except ValueError:
        return ""


def _json_reason(resp: httpx.Response) -> str:
    """Reddit explains 403/404 on subreddits as JSON: {"reason": "private"|"banned"|"quarantined"}."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    return str(data.get("reason") or "") if isinstance(data, dict) else ""


def _remaining(resp: httpx.Response) -> float | None:
    try:
        raw = resp.headers.get("x-ratelimit-remaining")
        return float(raw) if raw is not None else None
    except ValueError:
        return None


class RedditCollector(Collector):
    name = "reddit"
    label = "Reddit"
    signal_types = ("competitor_engagement", "keyword_mention")
    requires = ("Reddit API app credentials (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET; commercial use needs "
                "Reddit's agreement) plus keywords or competitors (optionally subreddits)")

    def __init__(self) -> None:
        # (client id, secret) -> (access token, monotonic expiry)
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    def is_configured(self, company: Company) -> bool:
        return _has_credentials(get_settings()) and bool(company.signals.keywords or company.competitors)

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        if not _has_credentials(ctx.settings):
            ctx.warn("Reddit: REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set; skipped.")
            return []
        terms = _company_terms(company)
        if not terms:
            return []
        subreddits = self._valid_subreddits(company.signals.subreddits, ctx)
        if company.signals.subreddits and not subreddits:
            return []
        since = parse_time(ctx.since) or ctx.since
        plan = _plan(terms, subreddits)
        if len(plan) > MAX_REQUESTS:
            ctx.warn(f"Reddit: only the first {MAX_REQUESTS} of {len(plan)} searches run per scan; "
                     "list fewer subreddits to cover them all.")

        token = await self._access_token(ctx)
        if not token:
            return []
        headers = {"Authorization": f"bearer {token}", "User-Agent": user_agent(ctx.settings),
                   "Accept": "application/json"}
        window = _time_filter(since)

        results: list[RawSignal] = []
        seen: set[str] = set()
        dead_scopes: set[str] = set()
        queue: deque[_Search] = deque(plan)
        sent = failures = bad_items = 0
        while queue and sent < MAX_REQUESTS and len(results) < ctx.max_items:
            search = queue.popleft()
            if search.scope in dead_scopes:
                continue
            sent += 1
            page = await self._fetch(ctx, search, headers, window)
            if page.outcome == STOP:
                break
            if page.outcome == SKIP_SCOPE:
                dead_scopes.add(search.scope)
                continue
            if page.outcome == FAILED:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    ctx.warn(f"Reddit: {failures} requests failed in a row; stopped for this scan.")
                    break
                continue
            failures = 0
            for post in page.posts:
                if len(results) >= ctx.max_items:
                    break
                try:
                    raw = _post_signal(post, terms, search, since)
                except Exception:  # one malformed post must not sink the scan
                    bad_items += 1
                    continue
                if raw is not None and raw.signal.external_id not in seen:
                    seen.add(raw.signal.external_id)
                    results.append(raw)
            if page.stop_after:
                break
            if self._should_page(page, since):
                queue.append(replace(search, after=page.after))
        if bad_items:
            ctx.warn(f"Reddit: skipped {bad_items} malformed post(s).")
        return results

    # ----------------------------------------------------------------------------------

    @staticmethod
    def _valid_subreddits(subreddits: list[str], ctx: CollectContext) -> list[str]:
        valid = [s for s in subreddits if _SUBREDDIT_RE.match(s)]
        if invalid := [s for s in subreddits if s not in valid]:
            ctx.warn(f"Reddit: ignored invalid subreddit name(s): {', '.join(invalid)[:200]}.")
        return valid

    @staticmethod
    def _should_page(page: _Page, since: datetime) -> bool:
        """Ask for the next page only when this one was full and still inside the lookback window."""
        if not page.after or page.count < PAGE_SIZE:
            return False
        times = [t for t in (parse_time(p.get("created_utc")) for p in page.posts) if t is not None]
        return bool(times) and min(times) > since

    async def _access_token(self, ctx: CollectContext) -> str | None:
        settings = ctx.settings
        key = (settings.reddit_client_id.strip(), settings.reddit_client_secret.strip())
        cached = self._tokens.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        try:
            resp = await ctx.client.post(
                TOKEN_URL, data={"grant_type": "client_credentials"}, auth=key,
                headers={"User-Agent": user_agent(settings), "Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            ctx.warn(f"Reddit: could not reach the token endpoint ({type(exc).__name__}); skipped this scan.")
            return None
        if resp.status_code != 200:
            hint = {401: "check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET",
                    403: "blocked, or the app is not approved for Data API access",
                    429: "rate limited"}.get(resp.status_code, "Reddit error")
            ctx.warn(f"Reddit: access token request failed (HTTP {resp.status_code}: {hint}); skipped this scan.")
            return None
        try:
            data = resp.json()
        except ValueError:
            data = None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token or not isinstance(token, str):
            error = data.get("error") if isinstance(data, dict) else "not JSON"
            ctx.warn(f"Reddit: token endpoint returned no access token ({error}); skipped this scan.")
            return None
        try:
            lifetime = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._tokens[key] = (token, time.monotonic() + max(0.0, lifetime - TOKEN_EXPIRY_MARGIN))
        return token

    async def _fetch(self, ctx: CollectContext, search: _Search, headers: dict[str, str], window: str) -> _Page:
        url = f"{API_BASE}/r/{search.scope}/search" if search.scope else f"{API_BASE}/search"
        params = {"q": search.query, "sort": "new", "t": window, "type": "link",
                  "limit": str(PAGE_SIZE), "raw_json": "1"}
        if search.scope:
            params["restrict_sr"] = "1"
        if search.after:
            params["after"] = search.after
        try:
            resp = await ctx.client.get(url, params=params, headers=headers, follow_redirects=False)
        except httpx.HTTPError as exc:
            ctx.warn(f"Reddit: search {search.label} failed ({type(exc).__name__}).")
            return _Page(FAILED)

        status = resp.status_code
        if status == 429:
            ctx.warn(f"Reddit: rate limited (HTTP 429{_reset_hint(resp)}); stopped for this scan.")
            return _Page(STOP)
        if status == 401:
            self._tokens.pop((ctx.settings.reddit_client_id.strip(), ctx.settings.reddit_client_secret.strip()), None)
            ctx.warn("Reddit: access token rejected (HTTP 401); stopped. A new token is requested next scan.")
            return _Page(STOP)
        if status == 403 or status == 404 or 300 <= status < 400:
            reason = _json_reason(resp)
            if status == 403 and not reason:
                ctx.warn("Reddit: request blocked (HTTP 403). Check that the app is approved for Data API "
                         "access; stopped for this scan.")
                return _Page(STOP)
            if search.scope:
                ctx.warn(f"Reddit: r/{search.scope} is unavailable ({reason or f'HTTP {status}'}); skipped it.")
                return _Page(SKIP_SCOPE)
        if status >= 300:
            ctx.warn(f"Reddit: search {search.label} failed (HTTP {status}).")
            return _Page(FAILED)

        try:
            data = resp.json()["data"]
            children = data["children"]
            if not isinstance(children, list):
                raise TypeError("children is not a list")
        except (ValueError, KeyError, TypeError):
            ctx.warn(f"Reddit: search {search.label} returned an unexpected response; skipped it.")
            return _Page(FAILED)
        posts = [c["data"] for c in children
                 if isinstance(c, dict) and c.get("kind") == "t3" and isinstance(c.get("data"), dict)]
        remaining = _remaining(resp)
        stop_after = remaining is not None and remaining < 1
        if stop_after:
            ctx.warn(f"Reddit: API rate limit used up{_reset_hint(resp)}; stopped for this scan.")
        return _Page(OK, posts, count=len(children), after=str(data.get("after") or ""), stop_after=stop_after)
