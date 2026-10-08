"""Hacker News collector, built on the Algolia HN Search API.

Source
    GET https://hn.algolia.com/api/v1/search_by_date  (newest first; no key, no signup)
    Parameters used: query, tags, numericFilters=created_at_i>{unix seconds}, hitsPerPage.
    `tags` are ANDed when comma separated and ORed inside parentheses, so
    `tags=(story,comment)` searches stories and comments in one request and
    `tags=comment,story_<id>` searches the comments of one thread.

What it emits
    keyword_mention        a story or comment about one of `signals.keywords`  -> person lead (the author)
    competitor_engagement  a story or comment about one of `competitors`        -> person lead (the author)
    hiring                 a top-level comment in the latest "Ask HN: Who is hiring?" thread (posted
                           monthly by the `whoishiring` account) that mentions one of
                           `signals.hiring_keywords`; the "Company | Role | Location" first line
                           becomes an account-level signal for that company.

Strength (50 = typical)
    mentions: 50 for a plain mention in the item's own text; 60 for an "Ask HN" question;
    75 when the text has a buying-intent phrase ("looking for", "recommend", "alternative to",
    "anyone use"...), 80 for an Ask HN with one; 85 when a competitor is mentioned together with
    a churn phrase ("switching from", "frustrated with", "too expensive"...). Comments that only sit
    in a thread about the term (the term is in the story title, not in the comment) are weak:
    40 for a competitor thread, 30 for a keyword thread, 60 if the comment itself shows intent.
    hiring: 60 when the keyword is in the job headline, 40 when it only appears in the body.

Limits and politeness
    Algolia allows 10,000 requests/hour per IP, sends no rate-limit headers and is reported to
    block abusive IPs instead of answering 429, so this collector is deliberately frugal: at most
    MAX_REQUESTS_PER_SCAN sequential requests per scan with a short pause between them, one page
    per query (the newest hits), and it stops at the first 429/403 or after a few failures in a row.
    When there are more terms than the request budget allows, the searched subset rotates daily.
    HN profiles carry no company or e-mail, so person leads are just the HN username plus profile
    URL; profile enrichment (user endpoints) is not done here because it is not part of the
    verified API research.

Terms of use
    HN content is public; Algolia's HN Search API is a free public service offered by Algolia.
    Keep the volume low, link back to news.ycombinator.com and do not republish content in bulk.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..models import Company, LeadIn, SignalIn
from .base import CollectContext, Collector, RawSignal, find_terms, parse_time, strip_html, truncate

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"
USER_URL = "https://news.ycombinator.com/user?id={user}"
SOURCE = "hackernews"

MAX_REQUESTS_PER_SCAN = 12
MAX_HIRING_KEYWORDS = 4          # hiring uses 1 request to find the thread + 1 per keyword
MENTION_HITS_PER_PAGE = 50
HIRING_HITS_PER_PAGE = 100
MAX_FAILURES_IN_A_ROW = 3
SUMMARY_LIMIT = 500
HIRING_BOT = "whoishiring"

# Phrases that suggest the author is shopping for a solution.
INTENT_PHRASES = [
    "looking for", "recommend", "recommendation", "recommendations", "any suggestions",
    "suggestions for", "anyone use", "anyone using", "anyone tried",
    "what do you use", "what are you using", "what's the best", "alternative to",
    "alternatives to", "switching from", "switch from", "migrating from", "moving away from",
    "replacement for", "frustrated with", "fed up with", "evaluating",
]
# Phrases that suggest an unhappy competitor customer (only boost competitor mentions).
CHURN_PHRASES = [
    "alternative to", "alternatives to", "switching from", "switch from", "switched from",
    "migrating from", "moving away from", "replacement for", "frustrated with", "fed up with",
    "cancel", "cancelled", "canceled", "too expensive", "price increase", "terrible", "awful",
]
# Hosts in a job headline that belong to job boards / tools, not to the hiring company.
NON_COMPANY_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "linkedin.com", "notion.site",
    "notion.so", "google.com", "forms.gle", "ycombinator.com", "workatastartup.com", "wellfound.com",
    "angel.co", "breezy.hr", "recruitee.com", "bamboohr.com", "smartrecruiters.com", "github.com",
    "typeform.com", "rippling.com", "jobvite.com", "teamtailor.com", "personio.de", "personio.com",
    "homerun.co", "bit.ly", "calendly.com", "airtable.com", "welcometothejungle.com", "dover.com",
)
_TYPE_RANK = {"competitor_engagement": 2, "keyword_mention": 1}
_DEAD_TEXTS = {"", "[deleted]", "[dead]", "[flagged]"}


class HackerNewsCollector(Collector):
    name = "hackernews"
    label = "Hacker News"
    signal_types = ("competitor_engagement", "keyword_mention", "hiring")
    requires = "keywords, competitors or hiring keywords"

    # Seconds between two API calls (tests set this to 0).
    request_interval: float = 0.5

    def is_configured(self, company: Company) -> bool:
        s = company.signals
        return bool(s.keywords or company.competitors or s.hiring_keywords)

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        enabled = set(company.signals.enabled_types)
        fetcher = _Fetcher(ctx, MAX_REQUESTS_PER_SCAN, self.request_interval)
        since_ts = int(ctx.since.timestamp())
        out: list[RawSignal] = []

        hiring_keywords = company.signals.hiring_keywords if "hiring" in enabled else []
        if hiring_keywords and ctx.max_items > 0:
            out += await _collect_hiring(hiring_keywords, ctx, fetcher, since_ts)

        terms = mention_terms(company, enabled)
        room = ctx.max_items - len(out)
        if terms and room > 0 and not fetcher.exhausted:
            out += await _collect_mentions(terms, ctx, fetcher, since_ts, room)
        return out[: ctx.max_items]


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class _Fetcher:
    """Sequential, capped access to the search endpoint. Never raises; warns instead."""

    def __init__(self, ctx: CollectContext, max_requests: int, interval: float) -> None:
        self.ctx = ctx
        self.max_requests = max_requests
        self.interval = interval
        self.used = 0
        self.failures_in_a_row = 0
        self.stopped = False

    @property
    def exhausted(self) -> bool:
        return self.stopped or self.used >= self.max_requests

    @property
    def remaining(self) -> int:
        return 0 if self.stopped else max(0, self.max_requests - self.used)

    async def search(self, params: dict[str, Any], what: str) -> list[dict[str, Any]] | None:
        """One search_by_date call. Returns the hits, or None when the request failed."""
        if self.exhausted:
            return None
        if self.used and self.interval > 0:
            await asyncio.sleep(self.interval)
        self.used += 1
        try:
            resp = await self.ctx.client.get(SEARCH_URL, params=params, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            return self._fail(f"Hacker News: search for {what} timed out")
        except Exception as exc:  # transport errors, invalid URLs...: one request must not sink the scan
            return self._fail(f"Hacker News: search for {what} failed ({type(exc).__name__})")
        if resp.status_code == 429:
            self._stop("Hacker News: rate limited by Algolia (HTTP 429); skipped the remaining searches this scan")
            return None
        if resp.status_code == 403:
            self._stop("Hacker News: Algolia refused access (HTTP 403, the IP may be blocked); "
                       "skipped the remaining searches this scan")
            return None
        if resp.status_code >= 400:
            return self._fail(f"Hacker News: HTTP {resp.status_code} for {what}")
        try:
            data = resp.json()
        except ValueError:
            return self._fail(f"Hacker News: invalid JSON for {what}")
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list):
            detail = f": {truncate(str(data['message']), 120)}" if isinstance(data, dict) and data.get("message") else ""
            return self._fail(f"Hacker News: unexpected response for {what}{detail}")
        self.failures_in_a_row = 0
        if resp.headers.get("x-ratelimit-remaining", "").strip() == "0":
            self._stop("Hacker News: Algolia rate-limit budget exhausted; skipped the remaining searches this scan")
        return [h for h in hits if isinstance(h, dict)]

    def _fail(self, message: str) -> None:
        self.ctx.warn(message)
        self.failures_in_a_row += 1
        if self.failures_in_a_row >= MAX_FAILURES_IN_A_ROW and not self.stopped:
            self._stop(f"Hacker News: {self.failures_in_a_row} failed requests in a row; stopped this scan")
        return None

    def _stop(self, message: str) -> None:
        self.stopped = True
        self.ctx.warn(message)


# --------------------------------------------------------------------------------------
# Keyword / competitor mentions
# --------------------------------------------------------------------------------------


def mention_terms(company: Company, enabled: set[str]) -> list[tuple[str, str]]:
    """(term, signal type) pairs, competitors and keywords interleaved so both get searched."""
    competitors = company.competitors if "competitor_engagement" in enabled else []
    competitor_keys = {c.lower() for c in competitors}
    keywords = [k for k in company.signals.keywords if k.lower() not in competitor_keys] \
        if "keyword_mention" in enabled else []
    out: list[tuple[str, str]] = []
    for i in range(max(len(competitors), len(keywords))):
        if i < len(competitors):
            out.append((competitors[i], "competitor_engagement"))
        if i < len(keywords):
            out.append((keywords[i], "keyword_mention"))
    return [(t, kind) for t, kind in out if len(t.strip()) >= 2]


async def _collect_mentions(terms: list[tuple[str, str]], ctx: CollectContext, fetcher: _Fetcher,
                            since_ts: int, room: int) -> list[RawSignal]:
    budget = fetcher.remaining
    if len(terms) > budget:
        # Rotate the searched subset daily so every term is covered over a few scans.
        start = ctx.since.date().toordinal() % len(terms)
        terms = terms[start:] + terms[:start]
        ctx.warn(f"Hacker News: {len(terms)} keywords/competitors but only {budget} searches left "
                 f"this scan (cap {MAX_REQUESTS_PER_SCAN}); searching {', '.join(t for t, _ in terms[:budget])}")

    found: dict[str, RawSignal] = {}
    for term, sig_type in terms:
        if len(found) >= room or fetcher.exhausted:
            break
        hits = await fetcher.search(
            {
                "query": term,
                "tags": "(story,comment)",
                "numericFilters": f"created_at_i>{since_ts}",
                "hitsPerPage": MENTION_HITS_PER_PAGE,
            },
            f"'{term}'",
        )
        for hit in hits or []:
            try:
                raw = mention_signal(hit, term, sig_type, ctx.since)
            except Exception as exc:  # a malformed hit must not sink the query
                ctx.warn(f"Hacker News: skipped a malformed result for '{term}' ({type(exc).__name__})")
                continue
            if raw is None:
                continue
            key = raw.signal.external_id
            if key in found:
                found[key] = _merge_mentions(found[key], raw)
            elif len(found) < room:
                found[key] = raw
    return list(found.values())


def mention_signal(hit: dict[str, Any], term: str, sig_type: str, since: datetime) -> RawSignal | None:
    """Turn one Algolia story/comment hit into a person-level signal, or None to skip it."""
    author = str(hit.get("author") or "").strip()
    object_id = str(hit.get("objectID") or "").strip()
    if not author or not object_id or author == HIRING_BOT or hit.get("dead") or hit.get("deleted"):
        return None
    occurred = parse_time(hit.get("created_at_i") or hit.get("created_at"))
    if occurred is None or occurred <= since:
        return None

    tags = hit.get("_tags") or []
    is_comment = "comment_text" in hit or "comment" in tags
    if is_comment:
        own_title = ""
        body = _text(hit.get("comment_text"))
        if body.lower() in _DEAD_TEXTS:
            return None
        context = " ".join([_text(hit.get("story_title")), str(hit.get("story_url") or "")])
    else:
        own_title = _text(hit.get("title"))
        body = _text(hit.get("story_text"))
        if own_title.lower() in _DEAD_TEXTS:
            return None
        context = str(hit.get("url") or "")
    own_text = f"{own_title} {body}".strip()

    pattern = term_pattern(term)
    if pattern.search(own_text):
        where = "text"
    elif pattern.search(context):
        where = "context"
    else:
        return None  # Algolia matched on the author name or fuzzily; not a real mention

    intents = find_terms(own_text, INTENT_PHRASES)
    churn = find_terms(own_text, CHURN_PHRASES) if sig_type == "competitor_engagement" else []
    is_ask = not is_comment and ("ask_hn" in tags or own_title.lower().startswith("ask hn"))
    strength = mention_strength(sig_type, where, bool(intents), bool(churn), is_ask)

    story_title = _text(hit.get("story_title"))
    if not is_comment:
        title = own_title if is_ask else f"Posted on HN: {own_title}"
        summary = _excerpt(body, pattern) if body else (
            f"{own_title} ({hit['url']})" if hit.get("url") else own_title)
    elif where == "text":
        title = f"Mentioned {term} on HN" + (f": \"{story_title}\"" if story_title else "")
        summary = _excerpt(body, pattern)
    else:
        title = f"Commented on HN thread \"{story_title or term}\""
        summary = _excerpt(body, None)

    raw_fields = {
        "hn_id": object_id,
        "kind": "comment" if is_comment else "story",
        "author": author,
        "query": term,
        "matched_in": where,
        "intent_phrases": intents + [p for p in churn if p not in intents],
        "story_id": hit.get("story_id"),
        "story_title": story_title or None,
        "points": hit.get("points"),
        "num_comments": hit.get("num_comments"),
        "link": hit.get("url") or hit.get("story_url") or None,
    }
    signal = SignalIn(
        type=sig_type,
        title=truncate(title, 160),
        summary=summary,
        url=ITEM_URL.format(id=object_id),
        source=SOURCE,
        external_id=f"hn:{object_id}",
        strength=strength,
        occurred_at=occurred,
        raw={k: v for k, v in raw_fields.items() if v not in (None, [], "")},
    )
    return RawSignal(signal=signal, lead=hn_lead(author))


def mention_strength(sig_type: str, where: str, has_intent: bool, has_churn: bool, is_ask: bool) -> int:
    if where != "text":  # only the thread is about the term
        if has_intent:
            return 60
        return 40 if sig_type == "competitor_engagement" else 30
    if sig_type == "competitor_engagement" and has_churn:
        return 85
    if has_intent:
        return 80 if is_ask else 75
    return 60 if is_ask else 50


def hn_lead(username: str) -> LeadIn:
    return LeadIn(full_name=username, profile_url=USER_URL.format(user=quote(username, safe="")),
                  source=SOURCE)


def _merge_mentions(a: RawSignal, b: RawSignal) -> RawSignal:
    """Same HN item found by two terms: keep the stronger type and record both terms."""
    keep, other = (b, a) if _TYPE_RANK.get(b.signal.type, 0) > _TYPE_RANK.get(a.signal.type, 0) else (a, b)
    also = [*keep.signal.raw.get("also_matched", []), other.signal.raw.get("query"),
            *other.signal.raw.get("also_matched", [])]
    also = [q for q in dict.fromkeys(also) if q and q != keep.signal.raw.get("query")]
    raw = {**keep.signal.raw, "also_matched": also}
    signal = keep.signal.model_copy(update={"strength": max(a.signal.strength, b.signal.strength), "raw": raw})
    return RawSignal(signal=signal, lead=keep.lead, account=keep.account, account_domain=keep.account_domain)


# --------------------------------------------------------------------------------------
# "Ask HN: Who is hiring?"
# --------------------------------------------------------------------------------------


@dataclass
class HiringThread:
    id: str
    title: str


async def _collect_hiring(keywords: list[str], ctx: CollectContext, fetcher: _Fetcher,
                          since_ts: int) -> list[RawSignal]:
    hits = await fetcher.search({"tags": f"story,author_{HIRING_BOT}", "hitsPerPage": 10},
                                "the latest 'Who is hiring?' thread")
    if hits is None:
        return []
    thread = latest_hiring_thread(hits)
    if thread is None:
        ctx.warn("Hacker News: could not find the latest 'Ask HN: Who is hiring?' thread")
        return []
    if len(keywords) > MAX_HIRING_KEYWORDS:
        ctx.warn(f"Hacker News: only the first {MAX_HIRING_KEYWORDS} of {len(keywords)} hiring keywords "
                 "are searched in 'Who is hiring?'")

    found: dict[str, RawSignal] = {}
    for keyword in keywords[:MAX_HIRING_KEYWORDS]:
        if fetcher.exhausted or len(found) >= ctx.max_items:
            break
        hits = await fetcher.search(
            {
                "query": keyword,
                "tags": f"comment,story_{thread.id}",
                "numericFilters": f"created_at_i>{since_ts}",
                "hitsPerPage": HIRING_HITS_PER_PAGE,
            },
            f"hiring '{keyword}'",
        )
        for hit in hits or []:
            try:
                raw = hiring_signal(hit, keyword, thread, ctx.since)
            except Exception as exc:
                ctx.warn(f"Hacker News: skipped a malformed hiring post ({type(exc).__name__})")
                continue
            if raw is None:
                continue
            key = raw.signal.external_id
            if key in found:
                prev = found[key].signal
                matched = list(dict.fromkeys([*prev.raw.get("matched", []), keyword]))
                found[key].signal = prev.model_copy(update={
                    "strength": max(prev.strength, raw.signal.strength), "raw": {**prev.raw, "matched": matched}})
            elif len(found) < ctx.max_items:
                found[key] = raw
    return list(found.values())


def latest_hiring_thread(hits: list[dict[str, Any]]) -> HiringThread | None:
    threads = [
        h for h in hits
        if h.get("objectID") and re.search(r"\bwho is hiring\b", str(h.get("title") or ""), re.I)
    ]
    if not threads:
        return None
    best = max(threads, key=_created_ts)
    return HiringThread(id=str(best["objectID"]), title=_text(best.get("title")))


def hiring_signal(hit: dict[str, Any], keyword: str, thread: HiringThread, since: datetime) -> RawSignal | None:
    """A top-level job post in the hiring thread -> account-level hiring signal."""
    object_id = str(hit.get("objectID") or "").strip()
    author = str(hit.get("author") or "").strip()
    if not object_id or not author or hit.get("dead") or hit.get("deleted"):
        return None
    parent = hit.get("parent_id")
    if parent is not None and str(parent) != thread.id:
        return None  # a reply under a job post, not a job post
    occurred = parse_time(hit.get("created_at_i") or hit.get("created_at"))
    if occurred is None or occurred <= since:
        return None
    comment_html = str(hit.get("comment_text") or "")
    body = _text(comment_html)
    if body.lower() in _DEAD_TEXTS:
        return None
    pattern = term_pattern(keyword)
    if not pattern.search(body):
        return None

    headline_html = re.split(r"<p>|\n", comment_html, maxsplit=1)[0]
    headline = _text(headline_html)
    parsed = parse_headline(headline, keyword)
    if parsed is None:
        return None
    company, role = parsed
    in_headline = bool(pattern.search(headline))

    signal = SignalIn(
        type="hiring",
        title=truncate(f"Hiring: {role}", 160),
        summary=_excerpt(body, pattern),
        url=ITEM_URL.format(id=object_id),
        source=SOURCE,
        external_id=f"hn:hiring:{object_id}",
        strength=60 if in_headline else 40,
        occurred_at=occurred,
        raw={
            "hn_id": object_id,
            "thread_id": thread.id,
            "thread_title": thread.title,
            "author": author,
            "headline": truncate(headline, 300),
            "matched": [keyword],
            "matched_in": "headline" if in_headline else "body",
        },
    )
    return RawSignal(signal=signal, account=company, account_domain=company_domain(headline_html))


def parse_headline(headline: str, keyword: str) -> tuple[str, str] | None:
    """'Acme (YC W20) | Executive Assistant | Dubai | Onsite' -> ('Acme', 'Executive Assistant')."""
    parts = [p.strip() for p in headline.split("|") if p.strip()]
    if len(parts) < 2:
        parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", headline) if p.strip()]
    if len(parts) < 2:
        return None
    company = _clean_company(parts[0])
    if not company:
        return None
    pattern = term_pattern(keyword)
    role = next((p for p in parts[1:] if pattern.search(p)), "") or keyword
    return company, truncate(role, 120)


def _clean_company(text: str) -> str:
    name = re.sub(r"\([^)]*\)", " ", text)                     # "(YC W20)", "(acme.com)"
    name = re.sub(r"https?://\S+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -–—:,.")
    name = re.sub(r"^(?:company|hiring)\s*:\s*", "", name, flags=re.I)
    if not name and "." in text:                               # headline starts with a bare domain
        name = re.sub(r"^https?://(www\.)?", "", text.strip()).split("/")[0]
    return name if 0 < len(name) <= 80 else ""


def company_domain(headline_html: str) -> str:
    """The hiring company's own domain from links in the job headline (job-board links ignored)."""
    text = html.unescape(headline_html)
    candidates = re.findall(r"""href=["']([^"']+)["']""", text) + re.findall(r"https?://[^\s<>\"')|]+", text)
    for url in candidates:
        try:
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue
        if "." in host and not any(host == h or host.endswith("." + h) for h in NON_COMPANY_HOSTS):
            return host
    return ""


# --------------------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------------------


_INLINE_TAG_RE = re.compile(r"</?(?:a|i|b|em|strong|code|span|u|s)\b[^>]*>", re.I)


def _text(value: Any) -> str:
    """Plain text from HN's HTML: inline tags vanish (no stray spaces), block tags become spaces."""
    return strip_html(_INLINE_TAG_RE.sub("", str(value or "")))


def term_pattern(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive match that tolerates plurals and hyphen/space variants."""
    words = [re.escape(w) for w in term.lower().split()]
    body = r"[\s\-]+".join(words) or re.escape(term)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?:'?s|es)?(?![a-z0-9])", re.I)


def _created_ts(hit: dict[str, Any]) -> float:
    when = parse_time(hit.get("created_at_i") or hit.get("created_at"))
    return when.timestamp() if when else 0.0


def _excerpt(text: str, pattern: re.Pattern[str] | None, limit: int = SUMMARY_LIMIT) -> str:
    """Up to `limit` chars of text, starting a little before the first match when it is far in."""
    if len(text) <= limit or pattern is None:
        return truncate(text, limit)
    m = pattern.search(text)
    if m is None or m.start() < limit // 3:
        return truncate(text, limit)
    start = m.start() - limit // 3
    space = text.find(" ", start)
    if 0 <= space < m.start():
        start = space + 1
    return truncate("…" + text[start:], limit)
