"""GitHub collector: people who open issues/PRs on, fork, or (your own repos only) star a watched repo.

Source: GitHub REST API (https://api.github.com, `X-GitHub-Api-Version: 2022-11-28`)
    GET /repos/{owner}/{repo}/issues?state=all&since=<iso>&sort=created&direction=desc&per_page=50
        Issues AND pull requests (PRs carry a `pull_request` key), newest first. GitHub's `since`
        filters on *updated_at*, so created_at is re-checked here.
    GET /repos/{owner}/{repo}/forks?sort=newest&per_page=30
    GET /repos/{owner}/{repo}/stargazers?per_page=100[&page=N]   Accept: application/vnd.github.star+json
        Only with GITHUB_TOKEN. Since July 2026 GitHub limits stargazer lists to repo admins and
        collaborators (they were being harvested for spam), so this only works for repos you own or
        collaborate on, i.e. your own inbound interest. Other repos answer 401/403/404 (or an empty
        list); we warn once and move on. Do NOT work around this restriction (no web-UI scraping, no
        GraphQL detours). The list is oldest-first, so the newest page is read from the Link header's
        rel="last" (page numbers only: URLs from response headers are never requested with the token).
    GET /users/{login}   public profile (name, company, blog, location, bio, X handle, public e-mail)
        for the strongest MAX_PROFILES people per scan, cached for the scan.

What it emits
    competitor_engagement  someone outside the project opened an issue or PR -> person lead (the author).
                           Skipped: bots (type "Bot" or a login ending in "[bot]"), GitHub's placeholder
                           accounts ("ghost" for deleted users, "Mannequin" for imports) and authors whose
                           author_association is OWNER, MEMBER or COLLABORATOR (they work there).
    github_star            someone forked the repo -> person lead (fork owner); a fork owned by an
                           organisation is an account-level signal for that organisation. Also someone
                           starred the repo (own/collaborator repos with GITHUB_TOKEN only) -> person lead.
    Insiders (each watched repo's owner account and anyone seen as OWNER/MEMBER/COLLABORATOR on a watched
    repo this scan) are dropped from forks and stars too: maintainers routinely fork to open PRs.

Strength (50 = typical)
    issues/PRs: 50 for an issue, 45 for a PR with no intent words; 80 when the title/body shows
        evaluation or migration intent ("evaluating", "alternative to", "migrating from", "pricing"...),
        75 for frustration ("frustrated", "deal breaker", "unmaintained"...), 70 for integration or
        production use ("integrate", "in production", "our team"...); +5 when two of those groups match
        (max 85). Issue-template HTML comments are ignored when matching. Everyday developer wording
        ("integration tests", "string comparison", "migrate to v2") deliberately does not count.
    forks: 50; 65 when the owner pushed commits to the fork after forking. Organisation forks: 60 / 70.
    stars: 50.

Limits and politeness
    At most MAX_REPOS repos per scan, one page per list (newest 50 issues/PRs, newest 30 forks, up to
    MAX_STAR_PAGES stargazer pages), sequential requests with a short pause, at most
    MAX_REQUESTS_ANONYMOUS requests without a token (GitHub allows 60/hour per IP) or
    MAX_REQUESTS_WITH_TOKEN with one (5,000/hour), and a time budget below services' collector timeout.
    It stops for the rest of the scan on HTTP 429, on a 403 rate-limit/secondary-limit answer, on a
    non-JSON 403 (blocked), when X-RateLimit-Remaining reaches 0, or after a few failures in a row.
    Profiles beyond the cap keep just the GitHub login and profile URL (merged by login on later scans).

Terms of use
    Uses only public, documented endpoints within GitHub's rate limits (GitHub Terms of Service and
    Acceptable Use Policies). GitHub forbids using its data for spam: only the public profile e-mail is
    stored (never commit e-mails), and outreach drafted from these leads must be relevant and personal.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..models import Company, LeadIn, SignalIn
from .base import CollectContext, Collector, RawSignal, find_terms, parse_time, strip_html, truncate

API = "https://api.github.com"
SOURCE = "github"
API_VERSION = "2022-11-28"
JSON_ACCEPT = "application/vnd.github+json"
STAR_ACCEPT = "application/vnd.github.star+json"

MAX_REPOS = 5
ISSUES_PER_PAGE = 50
FORKS_PER_PAGE = 30
STARS_PER_PAGE = 100
MAX_STAR_PAGES = 3                 # page 1 (to read the Link header) + the two newest pages
MAX_PROFILES = 25
MAX_REQUESTS_ANONYMOUS = 30        # GitHub allows 60/hour per IP without a token
MAX_REQUESTS_WITH_TOKEN = 60       # 5,000/hour with a token; stay frugal anyway
MAX_FAILURES_IN_A_ROW = 3
TIME_BUDGET_SECONDS = 90.0         # services.run_scan cancels a collector after 120 s
SUMMARY_LIMIT = 500
MATCH_LIMIT = 4000                 # characters of an issue body scanned for intent words

INSIDER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
GHOST_LOGIN = "ghost"              # GitHub's stand-in for deleted accounts (shared by all of them)
PLACEHOLDER_TYPES = {"Bot", "Mannequin"}  # Mannequin = placeholder for users of an imported repo

# Evaluating the project, or moving off another tool: the strongest buying signal on a repo.
# Generic developer wording is left out on purpose: "evaluation"/"comparison"/"comparing" match code
# ("lazy evaluation", "string comparison") and "migrate to"/"switching to" match upgrade PRs ("Migrate
# to Pydantic v2"); none of them says anything about buying.
EVALUATION_PHRASES = [
    "evaluating", "proof of concept", "poc", "pilot",
    "alternative to", "alternatives to", "migrating from", "migrate from", "migration from",
    "switching from", "switch from", "moving from",
    "moving away from", "replacement for", "replace our", "looking for", "pricing", "enterprise",
    "commercial license", "commercial support", "support contract",
]
# Frustration with the project: an unhappy user of a competitor is an opening.
FRUSTRATION_PHRASES = [
    "frustrated", "frustrating", "deal breaker", "dealbreaker", "showstopper", "blocking us",
    "unusable", "too slow", "too expensive", "abandoned", "unmaintained",
]
# Real use at work: integration, deployment and production questions. Bare "integration" and
# "deployment" are left out: they mostly mean "integration tests" / "deployment docs" on GitHub.
INTEGRATION_PHRASES = [
    "integrate", "integrating", "integration with", "in production", "production environment",
    "our team", "our company", "our customers", "our clients", "our platform", "our stack",
    "we are using", "we're using", "we use", "we rely on", "self-hosted", "self hosted",
    "deploy", "deployed", "deploying", "at scale", "sso", "saml", "on-prem", "on-premise",
]
_LINKEDIN_PROFILE_RE = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/(in|pub)/[^/?#\s]+", re.IGNORECASE)

# Hosts that are not an organisation's own domain (used when deriving an account domain).
NON_COMPANY_HOSTS = (
    "github.com", "github.io", "gitlab.io", "linkedin.com", "twitter.com", "x.com", "medium.com",
    "substack.com", "dev.to", "hashnode.dev", "notion.site", "notion.so", "netlify.app", "vercel.app",
    "pages.dev", "herokuapp.com", "blogspot.com", "wordpress.com", "about.me", "linktr.ee",
    "youtube.com", "facebook.com", "instagram.com", "discord.gg", "t.me",
)
FREEMAIL_DOMAINS = (
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
    "me.com", "proton.me", "protonmail.com", "pm.me", "gmx.de", "gmx.net", "web.de", "yandex.ru",
    "qq.com", "163.com", "users.noreply.github.com",
)


class GitHubCollector(Collector):
    name = "github"
    label = "GitHub issues, forks & stars"
    signal_types = ("competitor_engagement", "github_star")
    requires = "GitHub repos to watch (owner/repo); stargazers only for repos you admin, with GITHUB_TOKEN"

    # Seconds between two API calls and the overall time budget (tests shrink both).
    request_interval: float = 0.1
    time_budget: float = TIME_BUDGET_SECONDS

    def is_configured(self, company: Company) -> bool:
        return bool(company.signals.github_repos)

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        if ctx.max_items <= 0:
            return []
        enabled = set(company.signals.enabled_types)
        want_issues = "competitor_engagement" in enabled
        want_forks = "github_star" in enabled
        token = (ctx.settings.github_token or "").strip()

        repos = list(company.signals.github_repos)
        if len(repos) > MAX_REPOS:
            ctx.warn(f"GitHub: only the first {MAX_REPOS} of {len(repos)} repos are watched each scan "
                     f"({', '.join(repos[:MAX_REPOS])})")
            repos = repos[:MAX_REPOS]

        api = _GitHubApi(ctx, token, self.request_interval, self.time_budget)
        hits: dict[str, _Hit] = {}
        # Lower-case logins of people who work on a watched repo: never leads, whatever they did.
        insiders: set[str] = {repo.partition("/")[0].lower() for repo in repos}
        for repo in repos:
            if api.exhausted or len(hits) >= ctx.max_items:
                break
            repo_ok = True
            if want_issues:
                found, repo_ok = await _collect_issues(api, repo, insiders)
                _merge(hits, found)
            if want_forks and repo_ok and not api.exhausted:
                found, repo_ok = await _collect_forks(api, repo)
                _merge(hits, found)
                if token and repo_ok and not api.exhausted:
                    _merge(hits, await _collect_stars(api, repo))

        outsiders = [hit for hit in hits.values() if hit.login.lower() not in insiders]
        selected = sorted(outsiders, key=lambda h: (h.signal.strength, h.signal.occurred_at),
                          reverse=True)[: ctx.max_items]
        profiles = await _fetch_profiles(api, selected)
        out: list[RawSignal] = []
        for hit in selected:
            try:
                out.append(hit.to_raw(profiles.get(hit.login.lower())))
            except Exception as exc:  # a malformed profile must not sink the scan
                ctx.warn(f"GitHub: skipped a signal for {hit.login} ({type(exc).__name__})")
        return out


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


@dataclass
class _Reply:
    status: int
    data: Any = None
    last_page: int | None = None


class _GitHubApi:
    """Sequential, capped access to api.github.com. Never raises; warns instead."""

    def __init__(self, ctx: CollectContext, token: str, interval: float, time_budget: float) -> None:
        self.ctx = ctx
        self.since = ctx.since if ctx.since.tzinfo else ctx.since.replace(tzinfo=timezone.utc)
        self.token = token
        self.interval = interval
        self.max_requests = MAX_REQUESTS_WITH_TOKEN if token else MAX_REQUESTS_ANONYMOUS
        self.deadline = time.monotonic() + time_budget
        self.used = 0
        self.failures_in_a_row = 0
        self.stopped = False
        self._warned: set[str] = set()

    @property
    def exhausted(self) -> bool:
        return self.stopped or self.used >= self.max_requests

    def headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get(self, path: str, what: str, params: dict[str, Any] | None = None, *,
                  accept: str = JSON_ACCEPT, quiet: frozenset[int] = frozenset()) -> _Reply | None:
        """GET API + path. Returns the decoded reply, or None when the request failed or was skipped.

        Statuses in `quiet` are returned (without data) for the caller to handle instead of warned about.
        """
        if self.exhausted:
            return None
        if time.monotonic() > self.deadline:
            self._stop("GitHub: scan time budget used up; skipped the remaining requests")
            return None
        if self.used and self.interval > 0:
            await asyncio.sleep(self.interval)
        self.used += 1
        try:
            resp = await self.ctx.client.get(API + path, params=params, headers=self.headers(accept))
        except httpx.TimeoutException:
            return self.fail(f"GitHub: {what} timed out")
        except Exception as exc:  # transport errors etc.: one request must not sink the scan
            return self.fail(f"GitHub: {what} failed ({type(exc).__name__})")

        status = resp.status_code
        if self._rate_limited(resp):
            return None
        if status == 403 and _error_message(resp) is None:
            # Checked before `quiet`: an HTML block page is not GitHub saying "no access to this list".
            self._stop("GitHub: access refused (HTTP 403 without an API error, possibly blocked by a proxy "
                       "or GitHub's abuse detection); skipped the remaining requests this scan")
            return None
        if status in quiet:
            self.failures_in_a_row = 0
            self._check_remaining(resp)
            return _Reply(status)
        if 300 <= status < 400:
            return self.fail(f"GitHub: {what} was redirected (HTTP {status}); was the repository renamed?")
        if status == 401:
            self._stop("GitHub: GITHUB_TOKEN was rejected (HTTP 401); fix or remove it. Stopped this scan"
                       if self.token else f"GitHub: HTTP 401 for {what}; stopped this scan")
            return None
        if status == 403:
            return self.fail(f"GitHub: HTTP 403 for {what}: {_error_message(resp)}")
        if status >= 400:
            message = _error_message(resp)
            return self.fail(f"GitHub: HTTP {status} for {what}" + (f": {message}" if message else ""))
        try:
            data = resp.json()
        except ValueError:
            return self.fail(f"GitHub: invalid JSON for {what}")
        self.failures_in_a_row = 0
        self._check_remaining(resp)
        return _Reply(status, data, _last_page(resp))

    def warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.ctx.warn(message)

    def _rate_limited(self, resp: httpx.Response) -> bool:
        status = resp.status_code
        if status not in (403, 429):
            return False
        remaining = resp.headers.get("x-ratelimit-remaining", "").strip()
        retry_after = resp.headers.get("retry-after", "").strip()
        mentions_limit = "rate limit" in resp.text.lower()
        if status == 403 and remaining != "0" and not retry_after and not mentions_limit:
            return False
        if remaining == "0":
            self._stop(self._primary_limit_message(resp, f"HTTP {status}"))
        else:
            wait = f", retry after {retry_after}s" if retry_after.isdigit() else ""
            self._stop(f"GitHub: secondary rate limit hit (HTTP {status}{wait}); stopped this scan")
        return True

    def _check_remaining(self, resp: httpx.Response) -> None:
        if resp.headers.get("x-ratelimit-remaining", "").strip() == "0":
            self._stop(self._primary_limit_message(resp, "X-RateLimit-Remaining: 0"))

    def _primary_limit_message(self, resp: httpx.Response, why: str) -> str:
        reset = parse_time(_int(resp.headers.get("x-ratelimit-reset")))
        when = f", resets {reset:%H:%M} UTC" if reset else ""
        hint = "" if self.token else "; set GITHUB_TOKEN for 5,000 requests/hour"
        return f"GitHub: API rate limit used up ({why}{when}); stopped this scan{hint}"

    def fail(self, message: str) -> None:
        self.ctx.warn(message)
        self.failures_in_a_row += 1
        if self.failures_in_a_row >= MAX_FAILURES_IN_A_ROW and not self.stopped:
            self._stop(f"GitHub: {self.failures_in_a_row} failed requests in a row; stopped this scan")

    def _stop(self, message: str) -> None:
        if not self.stopped:
            self.stopped = True
            self.ctx.warn(message)


def _error_message(resp: httpx.Response) -> str | None:
    """GitHub's JSON error `message`, or None when the body is not a GitHub API error (e.g. HTML)."""
    try:
        data = resp.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("message"):
        return truncate(str(data["message"]), 140)
    return None


def _last_page(resp: httpx.Response) -> int | None:
    """Page number of the Link header's rel="last" (only the number: never follow header URLs)."""
    try:
        url = resp.links.get("last", {}).get("url")
        return int(httpx.URL(url).params.get("page", "")) if url else None
    except (ValueError, TypeError, httpx.InvalidURL):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_items(reply: _Reply | None, api: _GitHubApi, what: str) -> list[dict[str, Any]] | None:
    if reply is None:
        return None
    if not isinstance(reply.data, list):
        detail = f": {truncate(str(reply.data['message']), 120)}" \
            if isinstance(reply.data, dict) and reply.data.get("message") else ""
        api.fail(f"GitHub: unexpected response for {what}{detail}")
        return None
    return [item for item in reply.data if isinstance(item, dict)]


# --------------------------------------------------------------------------------------
# Hits
# --------------------------------------------------------------------------------------


@dataclass
class _Hit:
    """A signal plus the GitHub account (simple-user object) behind it, before profile enrichment."""

    signal: SignalIn
    actor: dict[str, Any] = field(default_factory=dict)

    @property
    def login(self) -> str:
        return str(self.actor.get("login") or "")

    @property
    def is_org(self) -> bool:
        return self.actor.get("type") == "Organization"

    def to_raw(self, profile: dict[str, Any] | None) -> RawSignal:
        if self.is_org:
            name = _clean(profile.get("name")) if profile else ""
            domain = company_domain(profile) if profile else ""
            return RawSignal(signal=self.signal, account=name or self.login, account_domain=domain)
        return RawSignal(signal=self.signal, lead=github_lead(self.actor, profile))


def _merge(hits: dict[str, _Hit], found: list[_Hit]) -> None:
    """Add hits by external_id; the same item seen twice (e.g. shifting pages) keeps the stronger copy."""
    for hit in found:
        key = hit.signal.external_id
        if key not in hits or hit.signal.strength > hits[key].signal.strength:
            hits[key] = hit


def is_bot(user: dict[str, Any]) -> bool:
    login = str(user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


def is_placeholder(user: dict[str, Any]) -> bool:
    """Bots, the shared 'ghost' of deleted accounts and imported 'Mannequin' users: not real people."""
    login = str(user.get("login") or "").strip().lower()
    return is_bot(user) or user.get("type") in PLACEHOLDER_TYPES or login == GHOST_LOGIN


def _actor(user: Any) -> dict[str, Any] | None:
    """The simple-user object if it is a usable account of a real person or organisation."""
    if not isinstance(user, dict) or not str(user.get("login") or "").strip() or is_placeholder(user):
        return None
    return user


# --------------------------------------------------------------------------------------
# Issues and pull requests -> competitor_engagement
# --------------------------------------------------------------------------------------


async def _collect_issues(api: _GitHubApi, repo: str, insiders: set[str]) -> tuple[list[_Hit], bool]:
    """Returns (hits, repo_ok). repo_ok is False when the repository does not exist.

    Adds the logins of OWNER/MEMBER/COLLABORATOR authors (on any listed item, old or new) to `insiders`.
    """
    since = api.since
    reply = await api.get(
        f"/repos/{_repo_path(repo)}/issues", f"issues of {repo}",
        {"state": "all", "since": _iso(since), "sort": "created", "direction": "desc",
         "per_page": ISSUES_PER_PAGE},
        quiet=frozenset({404, 410}),
    )
    if reply is not None and reply.status == 404:
        api.ctx.warn(f"GitHub: repository {repo} not found (HTTP 404); check the name in your signal settings")
        return [], False
    if reply is not None and reply.status == 410:
        api.ctx.warn(f"GitHub: issues are disabled on {repo}, so it has no issue authors to watch")
        return [], True
    hits: list[_Hit] = []
    for item in _as_items(reply, api, f"issues of {repo}") or []:
        insider = _insider_login(item)
        if insider:
            insiders.add(insider)
        try:
            hit = issue_hit(item, repo, since)
        except Exception as exc:  # a malformed item must not sink the list
            api.ctx.warn(f"GitHub: skipped a malformed issue on {repo} ({type(exc).__name__})")
            continue
        if hit is not None:
            hits.append(hit)
    return hits, True


def _insider_login(item: dict[str, Any]) -> str:
    """Lower-case login of an issue author who works on the repo (OWNER/MEMBER/COLLABORATOR), else ''."""
    user = item.get("user")
    if str(item.get("author_association") or "").upper() not in INSIDER_ASSOCIATIONS or not isinstance(user, dict):
        return ""
    return str(user.get("login") or "").strip().lower()


def issue_hit(item: dict[str, Any], repo: str, since: datetime) -> _Hit | None:
    """One issue/PR from the issues list -> competitor_engagement hit, or None to skip it."""
    actor = _actor(item.get("user"))
    number = item.get("number")
    if actor is None or number is None:
        return None
    association = str(item.get("author_association") or "").upper()
    if association in INSIDER_ASSOCIATIONS:
        return None
    occurred = parse_time(item.get("created_at"))
    if occurred is None or occurred < since:
        return None

    is_pr = isinstance(item.get("pull_request"), dict)
    title = _clean(item.get("title")) or f"#{number}"
    body = issue_text(item.get("body"))
    groups = intent_groups(f"{title}\n{body[:MATCH_LIMIT]}")
    strength = issue_strength(is_pr, groups)
    kind = "a PR" if is_pr else "an issue"
    issue_id = item.get("id")
    external_id = f"github:issue:{issue_id}" if issue_id else f"github:issue:{repo.lower()}#{number}"
    labels = [str(label.get("name")) for label in item.get("labels") or []
              if isinstance(label, dict) and label.get("name")]
    raw = {
        "repo": repo,
        "number": number,
        "kind": "pull_request" if is_pr else "issue",
        "author": actor["login"],
        "author_association": association or None,
        "state": item.get("state"),
        "comments": item.get("comments"),
        "labels": labels,
        "intent_phrases": [p for phrases in groups.values() for p in phrases],
        "intent": list(groups),
    }
    signal = SignalIn(
        type="competitor_engagement",
        title=truncate(f"Opened {kind} on {repo}: {title}", 160),
        summary=truncate(body or title, SUMMARY_LIMIT),
        url=str(item.get("html_url") or f"https://github.com/{repo}/issues/{number}"),
        source=SOURCE,
        external_id=external_id,
        strength=strength,
        occurred_at=occurred,
        raw={k: v for k, v in raw.items() if v not in (None, [], "")},
    )
    return _Hit(signal, actor)


_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")


def issue_text(body: Any) -> str:
    """Plain text of an issue body: template comments, images, headings markup and HTML removed."""
    text = _HTML_COMMENT_RE.sub(" ", str(body or ""))
    text = _IMAGE_RE.sub(" ", text)
    text = _HEADING_RE.sub("", text).replace("```", " ")
    return strip_html(text)


def intent_groups(text: str) -> dict[str, list[str]]:
    """Intent phrases found in `text`, by group (evaluation, frustration, integration)."""
    groups = {
        "evaluation": find_terms(text, EVALUATION_PHRASES),
        "frustration": find_terms(text, FRUSTRATION_PHRASES),
        "integration": find_terms(text, INTEGRATION_PHRASES),
    }
    return {name: hits for name, hits in groups.items() if hits}


def issue_strength(is_pr: bool, groups: dict[str, list[str]]) -> int:
    if "evaluation" in groups:
        strength = 80
    elif "frustration" in groups:
        strength = 75
    elif "integration" in groups:
        strength = 70
    else:
        return 45 if is_pr else 50
    return min(85, strength + 5) if len(groups) >= 2 else strength


# --------------------------------------------------------------------------------------
# Forks and stars -> github_star
# --------------------------------------------------------------------------------------


async def _collect_forks(api: _GitHubApi, repo: str) -> tuple[list[_Hit], bool]:
    reply = await api.get(f"/repos/{_repo_path(repo)}/forks", f"forks of {repo}",
                          {"sort": "newest", "per_page": FORKS_PER_PAGE}, quiet=frozenset({404}))
    if reply is not None and reply.status == 404:
        api.ctx.warn(f"GitHub: repository {repo} not found (HTTP 404); check the name in your signal settings")
        return [], False
    hits: list[_Hit] = []
    for item in _as_items(reply, api, f"forks of {repo}") or []:
        try:
            hit = fork_hit(item, repo, api.since)
        except Exception as exc:
            api.ctx.warn(f"GitHub: skipped a malformed fork of {repo} ({type(exc).__name__})")
            continue
        if hit is not None:
            hits.append(hit)
    return hits, True


def fork_hit(item: dict[str, Any], repo: str, since: datetime) -> _Hit | None:
    actor = _actor(item.get("owner"))
    fork_id = item.get("id")
    occurred = parse_time(item.get("created_at"))
    if actor is None or not fork_id or occurred is None or occurred < since:
        return None
    pushed = parse_time(item.get("pushed_at"))
    # A new fork inherits the parent's pushed_at; a later push means they are working on it.
    active = pushed is not None and pushed > occurred + timedelta(minutes=1)
    if actor.get("type") == "Organization":
        strength = 70 if active else 60
    else:
        strength = 65 if active else 50
    login = actor["login"]
    full_name = str(item.get("full_name") or f"{login}/{repo.split('/')[-1]}")
    summary = f"{login} forked {repo} as {full_name}"
    summary += f" and has pushed to it since (last push {pushed:%Y-%m-%d})." if active and pushed else "."
    description = _clean(item.get("description"))
    if description:
        summary += f" Description: {description}"
    raw = {
        "repo": repo,
        "kind": "fork",
        "fork": full_name,
        "owner": login,
        "owner_type": actor.get("type"),
        "pushed_after_fork": active,
        "pushed_at": item.get("pushed_at"),
        "language": item.get("language"),
    }
    signal = SignalIn(
        type="github_star",
        title=truncate(f"Forked {repo}", 160),
        summary=truncate(summary, SUMMARY_LIMIT),
        url=str(item.get("html_url") or f"https://github.com/{full_name}"),
        source=SOURCE,
        external_id=f"github:fork:{fork_id}",
        strength=strength,
        occurred_at=occurred,
        raw={k: v for k, v in raw.items() if v not in (None, [], "")},
    )
    return _Hit(signal, actor)


async def _collect_stars(api: _GitHubApi, repo: str) -> list[_Hit]:
    """Newest stargazers (needs a token and admin/collaborator access to the repo since July 2026)."""
    path = f"/repos/{_repo_path(repo)}/stargazers"
    denied = frozenset({401, 403, 404})
    pages: dict[int, list[dict[str, Any]]] = {}
    first = await api.get(path, f"stargazers of {repo}", {"per_page": STARS_PER_PAGE},
                          accept=STAR_ACCEPT, quiet=denied)
    if first is None:
        return []
    if first.status in denied:
        api.warn_once("stars-denied", f"GitHub: stargazer lists are only available for repos you administer "
                      f"or collaborate on (GitHub restricted them in July 2026); skipped stargazers of {repo} "
                      "and of any other repo you don't administer")
        return []
    items = _as_items(first, api, f"stargazers of {repo}")
    if items is None:
        return []
    pages[1] = items
    page = max(1, first.last_page or 1)
    hits: list[_Hit] = []
    requested = 1
    while page >= 1:
        if page not in pages:
            if requested >= MAX_STAR_PAGES or api.exhausted:
                break
            requested += 1
            reply = await api.get(path, f"stargazers of {repo} (page {page})",
                                  {"per_page": STARS_PER_PAGE, "page": page}, accept=STAR_ACCEPT)
            pages[page] = _as_items(reply, api, f"stargazers of {repo}") or []
        reached_older = not pages[page]
        for item in pages[page]:
            try:
                hit = star_hit(item, repo, api.since)
            except Exception as exc:
                api.ctx.warn(f"GitHub: skipped a malformed stargazer of {repo} ({type(exc).__name__})")
                continue
            if hit is None:
                starred = parse_time(item.get("starred_at"))
                reached_older = reached_older or (starred is not None and starred < api.since)
            else:
                hits.append(hit)
        if reached_older:
            break  # pages are oldest-first: everything before this page is older still
        page -= 1
    return hits


def star_hit(item: dict[str, Any], repo: str, since: datetime) -> _Hit | None:
    actor = _actor(item.get("user"))
    occurred = parse_time(item.get("starred_at"))
    if actor is None or occurred is None or occurred < since:
        return None
    login = actor["login"]
    user_key = actor.get("id") or login.lower()
    signal = SignalIn(
        type="github_star",
        title=truncate(f"Starred {repo}", 160),
        summary=f"{login} starred {repo} on {occurred:%Y-%m-%d}.",
        url=f"https://github.com/{repo}/stargazers",
        source=SOURCE,
        external_id=f"github:star:{repo.lower()}:{user_key}",
        strength=50,
        occurred_at=occurred,
        raw={"repo": repo, "kind": "star", "user": login},
    )
    return _Hit(signal, actor)


# --------------------------------------------------------------------------------------
# Profiles -> leads
# --------------------------------------------------------------------------------------


async def _fetch_profiles(api: _GitHubApi, hits: list[_Hit]) -> dict[str, dict[str, Any] | None]:
    """GET /users/{login} for the strongest distinct accounts (hits are sorted), cached for the scan."""
    profiles: dict[str, dict[str, Any] | None] = {}
    logins: dict[str, str] = {}
    for hit in hits:
        logins.setdefault(hit.login.lower(), hit.login)
    for key, login in logins.items():
        if len(profiles) >= MAX_PROFILES or api.exhausted:
            break
        reply = await api.get(f"/users/{quote(login, safe='')}", f"profile of {login}",
                              quiet=frozenset({404}))  # 404: account deleted or renamed
        data = reply.data if reply is not None else None
        profiles[key] = data if isinstance(data, dict) and data.get("login") else None
    if len(profiles) < len(logins) and not api.stopped:  # a stop has already been reported
        api.ctx.warn(f"GitHub: looked up {len(profiles)} of {len(logins)} profiles this scan "
                     f"(cap {MAX_PROFILES} or request budget); the rest keep just their GitHub username")
    return profiles


def github_lead(user: dict[str, Any], profile: dict[str, Any] | None) -> LeadIn:
    """Person lead from a simple-user object, enriched with the public profile when available."""
    login = str(user.get("login") or "").strip()
    p = profile or {}
    profile_url = _clean(p.get("html_url")) or _clean(user.get("html_url")) or f"https://github.com/{login}"
    email = _clean(p.get("email"))
    blog = normalize_website(p.get("blog"))
    # Many people put their LinkedIn profile in `blog`: it is an identity (merges with LinkedIn-sourced
    # leads and enables the LinkedIn channel), not their website.
    linkedin = blog if _LINKEDIN_PROFILE_RE.match(blog) else ""
    return LeadIn(
        full_name=_clean(p.get("name")) or login,
        github_username=login,
        profile_url=profile_url,
        linkedin_url=linkedin,
        lead_company=clean_company(p.get("company")),
        website="" if linkedin else blog,
        location=_clean(p.get("location")),
        bio=truncate(_clean(p.get("bio")), SUMMARY_LIMIT),
        twitter=_clean(p.get("twitter_username")).lstrip("@"),
        email=email if "@" in email and not email.lower().endswith("noreply.github.com") else "",
        source=SOURCE,
    )


def clean_company(value: Any) -> str:
    """GitHub's free-text company: '@northwind-logistics' -> 'northwind-logistics' (first org handle)."""
    text = _clean(value)
    if text.startswith("@"):
        return text.split()[0].lstrip("@").strip(",;")
    return text


def normalize_website(value: Any) -> str:
    """`blog` is '' when unset and often has no scheme: 'example.com' -> 'https://example.com'."""
    text = _clean(value)
    if not text or " " in text:
        return ""
    return text if re.match(r"^https?://", text, re.IGNORECASE) else f"https://{text}"


def company_domain(profile: dict[str, Any]) -> str:
    """An organisation's own domain from its blog URL, else from a non-freemail public e-mail."""
    website = normalize_website(profile.get("blog"))
    try:
        host = (urlsplit(website).hostname or "").lower().removeprefix("www.") if website else ""
    except ValueError:
        host = ""
    if "." in host and not _is_host_in(host, NON_COMPANY_HOSTS):
        return host
    email = _clean(profile.get("email")).lower()
    domain = email.rpartition("@")[2] if "@" in email else ""
    if "." in domain and not _is_host_in(domain, FREEMAIL_DOMAINS):
        return domain
    return ""


def _is_host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _repo_path(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value not in (None, "") else ""
