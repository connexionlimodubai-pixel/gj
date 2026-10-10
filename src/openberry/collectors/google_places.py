"""Google Maps businesses: companies found with Google Maps searches, with the contact details of their own websites.

Source (a Google Maps Platform API key with "Places API (New)" turned on; OPENBERRY_GOOGLE_PLACES_KEY or the
dashboard's API keys page)
    POST https://places.googleapis.com/v1/places:searchText  (Text Search (New))
        headers  X-Goog-Api-Key: <key>   (a header, so the key never appears in a URL or a log line)
                 X-Goog-FieldMask: places.id,places.websiteUri,nextPageToken   (required: no default field list)
        body     {"textQuery": "<search>", "pageSize": 20, "includePureServiceAreaBusinesses": true
                  [, "pageToken": "<nextPageToken of the previous page>"]}
    One request per `signals.places_queries` entry and page. pageSize is at most 20; a next page repeats every
    other parameter exactly (otherwise Google answers INVALID_ARGUMENT) and follows `nextPageToken` while there is
    one, at most MAX_PAGES_PER_QUERY pages (60 businesses) per search. Pure service-area businesses (event
    planners or DMCs without a storefront) are included. languageCode/regionCode only change how place details are
    displayed (we display none) and locationBias needs coordinates, so neither is sent: put the place in the search
    text ("event management companies in Dubai").
    Billing: a request bills at the highest SKU of the fields it asks for. websiteUri is a Text Search Enterprise
    field, so each request (one page) is one Enterprise event; Google gives 1,000 of those free a month (then
    $35 per 1,000). The API keys page checks a key with an IDs-only search (mask places.id), which costs nothing
    and is not counted.

What it emits
    business_search  one per business whose own website could be read: an account lead (no person yet) with the
                     name, website, domain, best email, first phone and meta description its website publishes
                     (sitecontacts.py), the location from the search text ("... in Dubai" -> "Dubai", else an ICP
                     location the search names), profile_url = a Google Maps link built from the Place ID, and
                     other emails/phones in the notes. Title "Found on Google Maps: “<search>”", url = the Maps
                     link, external_id "gp:<place id>". The lead's identity keys include acct:gp:<place id>, so the
                     same business found by two searches is one lead; it also merges by name and domain with
                     accounts found by other sources, but never by name alone with a lead that has another Place
                     ID. A page inside a bigger site (a hotel on its chain's site, a firm's Dubai office page)
                     gets no domain and its own name, so a chain's hotels don't merge into one lead.
    Skipped (counted in the scan stats, never stored): no website, or a social/marketplace page as website
    ("no_website"); robots.txt keeps us out or can't be reached ("robots"); the site doesn't answer, isn't HTML or
    isn't public ("unreachable"); your own website, a competitor or a never-contact company ("excluded").

Strength
    Always STRENGTH (10), type weight 10: being on Google Maps is not intent. These leads are a prospect list,
    scored mostly on ICP fit (cold or warm on their own), and business_search never counts toward signal stacking.

Time
    occurred_at is when the business was found. Each search is read in full at most once every
    RESEARCH_AFTER_DAYS days (table place_searches); a search interrupted before its last page runs again on the
    next scan. A Place ID that became a lead is never visited again, even after the lead is deleted; one that was
    skipped is checked again after RECHECK_SKIPPED_DAYS (table place_ids). Skips are recorded here, leads by
    services.ingest when it stores them, so a scan that is stopped or times out before that loses no business.

Limits and politeness
    Every request is counted before it is sent, failed ones too (Google may bill them), except the ones Google
    refuses for the key, billing or the API being off, which are never billed and are given back (otherwise a
    first-run setup problem would use up the month scan after scan): a global monthly cap (api_usage, BEGIN IMMEDIATE,
    so processes never go over it together; OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT, default 900 of Google's free
    1,000: Google's month starts at midnight Pacific time, 7-8 hours after ours) and at most MAX_REQUESTS_PER_SCAN
    per scan. A search starts only when all its pages fit in the scan. PAGE_INTERVAL seconds before a next page.
    Websites: SITE_CONCURRENCY at once, robots.txt honoured, public addresses only, size and time caps
    (sitecontacts.py). The collector has its own TIMEOUT_SECONDS (reading up to 200 websites doesn't fit the shared
    120 s) and starts nothing new after its time budget. An invalid key, a Places API or billing that is off, a
    refused key or Google's own quota stop the collector for the scan; two failed requests in a row too.

Terms of use
    Google Maps Platform Terms §3.2.3: no scraping, exporting or caching of Google Maps Content, except Place IDs,
    which may be stored indefinitely. So the Place ID is the only thing kept from Google (in place_ids, the lead's
    Maps link and identity key, and the signal); the websiteUri Google returns is used only to open the business's
    site during the scan, and everything stored about the business comes from its own website. The link back is
    labelled "Google Maps". Whether a list of businesses for outreach suits your use of Google Maps Platform is for
    you to check in Google's terms.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from .. import config, repo, scoring
from ..config import Settings, get_settings
from ..models import Company, LeadIn, SignalIn
from ..netguard import public_client
from ..sitecontacts import PAGE_TIMEOUT, SiteContacts, SiteReader, SiteSkipped, same_site
from .base import CollectContext, Collector, RawSignal, truncate

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.websiteUri,nextPageToken"   # bills as Text Search Enterprise (websiteUri)
CHECK_FIELD_MASK = "places.id"                              # Text Search Essentials (IDs Only): no charge
SERVICE = "google_places_search"                            # api_usage.service
GOOGLE_FREE_PER_MONTH = 1000
PAGE_SIZE = 20
MAX_PAGES_PER_QUERY = 3
MAX_REQUESTS_PER_SCAN = 10
RESEARCH_AFTER_DAYS = 7
RECHECK_SKIPPED_DAYS = 30
SITE_CONCURRENCY = 6
REQUEST_TIMEOUT = 20.0
TIMEOUT_SECONDS = 300.0
TIME_BUDGET_SECONDS = 270.0
PAGE_INTERVAL = 2.0
MIN_SECONDS_FOR_A_PAGE = 60.0
MIN_SECONDS_FOR_A_SITE = 5.0
MAX_FAILURES_IN_A_ROW = 2
STRENGTH = 10
TITLE_LIMIT = 160
SUMMARY_LIMIT = 500
BIO_LIMIT = 300
MESSAGE_LIMIT = 200
NO_KEY = "Google Maps: no API key. Add it on the API keys page."
LIMIT_OFF = "Google Maps: searches are off because the monthly limit is 0. Change it on the API keys page."
_LOCATION_WORDS = re.compile(r"\s(?:in|near|around)\s+", re.I)
_DENIED_REASONS = ("API_KEY_SERVICE_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED",
                   "API_KEY_ANDROID_APP_BLOCKED", "API_KEY_IOS_APP_BLOCKED")
_QUOTA_REASONS = ("RATE_LIMIT_EXCEEDED", "RESOURCE_QUOTA_EXCEEDED")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Google's Text Search (New)
# --------------------------------------------------------------------------------------


@dataclass
class SearchPage:
    places: list[tuple[str, str]]   # (place_id, website_uri or ""), in Google's order, unique, valid IDs only
    next_page_token: str


class PlacesError(Exception):
    """kind: "key" (stop: the key is invalid), "denied" (stop: API off, billing off, key restricted),
    "quota" (stop: Google's own quota), "bad_request" (stop this search), "failed" (counts toward 2 in a row)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def request_body(query: str, page_token: str = "", page_size: int = PAGE_SIZE) -> dict[str, Any]:
    body: dict[str, Any] = {"textQuery": query, "pageSize": page_size, "includePureServiceAreaBusinesses": True}
    if page_token:
        body["pageToken"] = page_token
    return body


def request_headers(key: str, mask: str = FIELD_MASK) -> dict[str, str]:
    return {"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": mask}


def parse_search_response(data: Any) -> SearchPage:
    """Missing "places" means no results. Entries without a valid id are dropped; anything we didn't ask for
    is ignored and never copied anywhere."""
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    places: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in data.get("places") or []:
        if not isinstance(item, dict):
            continue
        place_id = item.get("id")
        if not isinstance(place_id, str) or not repo.PLACE_ID_RE.fullmatch(place_id) or place_id in seen:
            continue
        uri = item.get("websiteUri")
        uri = uri.strip() if isinstance(uri, str) and re.match(r"https?://", uri.strip(), re.I) else ""
        seen.add(place_id)
        places.append((place_id, uri))
    token = data.get("nextPageToken")
    return SearchPage(places, token.strip() if isinstance(token, str) else "")


def _error_info(resp: httpx.Response) -> tuple[str, str, str]:
    """(status, reason, message) of an AIP-193 error body; empty strings when it is something else."""
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return "", "", ""
    if not isinstance(error, dict):
        return "", "", ""
    reason = ""
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("google.rpc.ErrorInfo"):
            reason = str(detail.get("reason") or "")
            break
    return str(error.get("status") or ""), reason, " ".join(str(error.get("message") or "").split())


def places_error(resp: httpx.Response, query: str = "") -> PlacesError:
    """Map an error response (AIP-193 JSON, or anything else) to a PlacesError with a short user message."""
    code = resp.status_code
    status, reason, message = _error_info(resp)
    if reason == "API_KEY_INVALID":
        return PlacesError("key", "Google Maps: Google says the API key is not valid. Check it on the API keys page.")
    if reason == "SERVICE_DISABLED":
        return PlacesError("denied", "Google Maps: Places API (New) is off for this key's Google Cloud project. "
                                     "Turn it on, then scan again.")
    if reason == "BILLING_DISABLED":
        return PlacesError("denied", "Google Maps: billing is off for this key's Google Cloud project. Google needs "
                                     "billing even for free searches.")
    if code == 429 or status == "RESOURCE_EXHAUSTED" or reason in _QUOTA_REASONS:
        return PlacesError("quota", f"Google Maps: Google's limit for this key was reached (HTTP {code}). "
                                    "Searches stopped for this scan.")
    if reason in _DENIED_REASONS or code in (401, 403):
        return PlacesError("denied", f"Google Maps: Google refused the key ({reason or f'HTTP {code}'}). "
                                     "Allow Places API (New) in the key's restrictions.")
    if code == 400:
        detail = f"{status or 'HTTP 400'}: {message[:MESSAGE_LIMIT]}" if message else (status or "HTTP 400")
        return PlacesError("bad_request", f"Google Maps: Google rejected the search “{query}” ({detail}).")
    return PlacesError("failed", f"Google Maps: the search “{query}” failed (HTTP {code}).")


async def search_page(client: httpx.AsyncClient, key: str, query: str, page_token: str = "") -> SearchPage:
    """One Text Search (New) request. Raises PlacesError; never includes the key in a message."""
    try:
        try:
            resp = await client.post(SEARCH_URL, json=request_body(query, page_token), headers=request_headers(key),
                                     timeout=REQUEST_TIMEOUT)
        except httpx.TimeoutException as exc:
            raise PlacesError("failed", f"Google Maps: the search “{query}” failed (timed out).") from exc
        except httpx.HTTPError as exc:
            raise PlacesError("failed", f"Google Maps: the search “{query}” failed ({type(exc).__name__}).") from exc
        if resp.status_code != 200:
            raise places_error(resp, query)
        try:
            return parse_search_response(resp.json())
        except ValueError as exc:
            raise PlacesError("failed", f"Google Maps: the search “{query}” failed (Google's answer was not "
                                        "readable).") from exc
    except PlacesError as exc:
        if key and key in exc.message:
            exc.message = exc.message.replace(key, "…")
        raise


async def check_key(key: str, client: httpx.AsyncClient | None = None) -> tuple[bool | None, str]:
    """Free check of a key: one IDs-only search (mask places.id, pageSize 1, textQuery "hotel").

    (True, "") accepted; (False, reason) refused; (None, why) Google couldn't be reached. Not counted in
    api_usage: IDs-only searches have no charge.
    """
    own = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"User-Agent": get_settings().user_agent})
    try:
        resp = await client.post(SEARCH_URL, json=request_body("hotel", page_size=1),
                                 headers=request_headers(key, CHECK_FIELD_MASK), timeout=REQUEST_TIMEOUT)
    except httpx.TimeoutException:
        return None, "it didn't answer in time"
    except httpx.HTTPError as exc:
        return None, type(exc).__name__
    finally:
        if own:
            await client.aclose()
    if resp.status_code == 200:
        return True, ""
    error = places_error(resp)
    if error.kind in ("key", "denied", "bad_request"):
        reason = error.message.removeprefix("Google Maps: ")
        return False, reason.replace(key, "…") if key else reason
    return None, f"HTTP {resp.status_code}"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def query_key(query: str) -> str:
    return " ".join(query.casefold().split())


def query_location(query: str, icp_locations: list[str]) -> str:
    """Where the search looks: the text after the last " in "/" near "/" around " ("hotels in Business Bay, Dubai"
    -> "Business Bay, Dubai"), else the first ICP location the search mentions, else ""."""
    text = " ".join(query.split())
    best = ""
    for m in _LOCATION_WORDS.finditer(text):
        best = text[m.end():]
    best = best.strip(" ,.;:-")
    if best:
        return best[:100]
    return next((loc for loc in icp_locations if scoring.location_matches(loc, text)), "")


def due_queries(queries: list[str], last: dict[str, datetime], now: datetime) -> list[str]:
    """Searches to run now: never read first (profile order), then the oldest; skip those read in the last
    RESEARCH_AFTER_DAYS days."""
    fresh = [q for q in queries if query_key(q) not in last]
    stale = [q for q in queries if query_key(q) in last
             and now - last[query_key(q)] >= timedelta(days=RESEARCH_AFTER_DAYS)]
    stale.sort(key=lambda q: last[query_key(q)])
    return fresh + stale


def _domain_like(value: str) -> str:
    value = value.strip().lower()
    return repo.normalize_domain(value) if "." in value and " " not in value else ""


def excluded(company: Company, site: SiteContacts) -> bool:
    """Your own website, a competitor, or a never-contact company (by domain, or by name: the same name without
    "LLC", "FZE"..., or every word of the entry in the site's name)."""
    own = _domain_like(company.website)
    name_key = repo.company_key(site.name)
    if (own and same_site(site.host, own)) or (name_key and name_key == repo.company_key(company.name)):
        return True
    for entry in [*company.competitors, *company.icp.exclude_companies]:
        if domain := _domain_like(entry):
            if same_site(site.host, domain):
                return True
        elif entry.strip() and name_key and (repo.company_key(entry) == name_key
                                             or scoring.phrase_in(entry, site.name)):
            return True
    return False


def build_signal(place_id: str, query: str, location: str, site: SiteContacts, now: datetime) -> RawSignal:
    name = site.name or site.host
    maps = repo.maps_place_url(place_id)
    email = site.emails[0] if site.emails else ""
    phone = site.phones[0] if site.phones else ""
    others = [*site.emails[1:], *site.phones[1:]]
    lead = LeadIn(lead_company=name[:200], company_domain=site.domain, website=site.url, email=email, phone=phone,
                  location=location, profile_url=maps, bio=truncate(site.description, BIO_LIMIT),
                  source="google_places",
                  notes=f"Other contacts on its website: {', '.join(others)}" if others else "")
    listed = " and ".join(x for x in (email, phone) if x)
    summary = (f"{name} came up in your Google Maps search “{query}”. "
               + (f"Its website lists {listed}." if listed else "Its website lists no email address or phone number."))
    signal = SignalIn(type="business_search", title=truncate(f"Found on Google Maps: “{query}”", TITLE_LIMIT),
                      summary=truncate(summary, SUMMARY_LIMIT), url=maps, source="google_places",
                      external_id=f"gp:{place_id}", strength=STRENGTH, occurred_at=now,
                      raw={"query": query, "place_id": place_id, "site": site.url, "pages": site.pages,
                           "emails": site.emails, "phones": site.phones, "shared_site": site.shared})
    return RawSignal(signal=signal, lead=lead)


def first_of_next_month(now: datetime) -> date:
    now = now.astimezone(timezone.utc)
    return date(now.year + (now.month == 12), now.month % 12 + 1, 1)


def day_month(day: date | str) -> str:
    """date(2026, 11, 1) or "2026-11-01" -> "1 November"."""
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            return day
    return f"{day.day} {_MONTHS[day.month - 1]}"


def limit_message(usage: repo.ApiUsage, now: datetime) -> str:
    if usage.limit <= 0:
        return LIMIT_OFF
    return (f"Google Maps: this month's limit of {usage.limit:,} searches is used up. Searches start again on "
            f"{day_month(first_of_next_month(now))}.")


def usage_summary(settings: Settings | None = None, now: datetime | None = None) -> dict[str, Any]:
    """This month's Google Maps searches, for the dashboard and get_company_profile. Never contains the key."""
    config.refresh_saved_settings()
    settings = settings or get_settings()
    now = now or _utcnow()
    usage = repo.api_usage(SERVICE, settings.google_places_monthly_limit, now)
    return {
        "searches_used_this_month": usage.used,
        "monthly_limit": usage.limit,
        "month": usage.month,
        "resets_on": first_of_next_month(now).isoformat(),
        "api_key_set": bool(settings.google_places_key),
        "searches_per_scan": MAX_REQUESTS_PER_SCAN,
    }


def _site_client(ctx: CollectContext) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """netguard.public_client for business websites (tests swap this out, like news._feed_client)."""
    return public_client(headers={"User-Agent": ctx.settings.user_agent}, timeout=PAGE_TIMEOUT)


def _left(deadline: float) -> float:
    return deadline - time.monotonic()


def _count(ctx: CollectContext, name: str, n: int = 1) -> None:
    if n > 0:
        ctx.count(name, n)


# --------------------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------------------


class GooglePlacesCollector(Collector):
    name = "google_places"
    label = "Google Maps businesses"
    signal_types = ("business_search",)
    # No env var names here: profile dumps are checked for leaked secrets (see reddit.py).
    requires = "places_queries plus a Google Maps API key, which you add on the API keys page"
    timeout_seconds = TIMEOUT_SECONDS

    time_budget: float = TIME_BUDGET_SECONDS  # no new Google request or website visit starts after this
    page_interval: float = PAGE_INTERVAL      # seconds before a next-page request (tests set 0)

    def is_configured(self, company: Company) -> bool:
        return bool(get_settings().google_places_key) and bool(company.signals.places_queries)

    def enabled_for(self, company: Company) -> bool:
        return self.is_configured(company)  # the searches are the opt-in; PROSPECT_TYPES aren't in enabled_types

    def usage(self) -> dict[str, Any]:
        return usage_summary()

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        settings, queries = ctx.settings, company.signals.places_queries
        if not queries or ctx.max_items <= 0:
            return []
        key = settings.google_places_key.strip()
        if not key:
            ctx.warn(NO_KEY)
            return []
        limit = settings.google_places_monthly_limit
        if limit <= 0:
            ctx.warn(LIMIT_OFF)
            return []
        now = _utcnow()
        deadline = time.monotonic() + self.time_budget
        await asyncio.to_thread(repo.prune_place_searches, company.id, {query_key(q) for q in queries})
        last = await asyncio.to_thread(repo.place_search_times, company.id)
        due = due_queries(queries, last, now)
        _count(ctx, "searches_not_due", len(queries) - len(due))
        if not due:
            return []
        out: list[RawSignal] = []
        seen: set[str] = set()
        used = failures = 0
        async with _site_client(ctx) as site_client:
            reader = SiteReader(site_client, deadline=deadline)
            for index, query in enumerate(due):
                # Start a search only when all its pages fit in this scan (re-reading page 1 later costs again).
                no_room = used > 0 and MAX_REQUESTS_PER_SCAN - used < MAX_PAGES_PER_QUERY
                if no_room or _left(deadline) < MIN_SECONDS_FOR_A_PAGE or len(out) >= ctx.max_items:
                    _count(ctx, "searches_waiting", len(due) - index)
                    break
                location = query_location(query, company.icp.locations)
                token, found, added, complete = "", 0, 0, False
                for page in range(MAX_PAGES_PER_QUERY):
                    if used >= MAX_REQUESTS_PER_SCAN or _left(deadline) < MIN_SECONDS_FOR_A_PAGE:
                        break
                    usage = await asyncio.to_thread(repo.reserve_api_call, SERVICE, limit)
                    if not usage.granted:
                        ctx.warn(limit_message(usage, now))
                        return self._finish(out, ctx)
                    used += 1
                    ctx.count("searches")
                    if page and self.page_interval:
                        await asyncio.sleep(self.page_interval)
                    try:
                        result = await search_page(ctx.client, key, query, token)
                    except PlacesError as exc:
                        ctx.warn(exc.message)
                        if exc.kind in ("key", "denied"):  # refused before the search ran: never billed
                            await asyncio.to_thread(repo.release_api_call, SERVICE, usage.month)
                        if exc.kind in ("key", "denied", "quota"):
                            return self._finish(out, ctx)
                        if exc.kind == "failed":
                            failures += 1
                            if failures >= MAX_FAILURES_IN_A_ROW:
                                ctx.warn(f"Google Maps: {failures} failed searches in a row; stopped for this scan.")
                                return self._finish(out, ctx)
                        break  # this search is not marked as read: it runs again on the next scan
                    failures = 0
                    found += len(result.places)
                    _count(ctx, "businesses", len(result.places))
                    fresh = [p for p in result.places if p[0] not in seen]
                    seen.update(pid for pid, _ in fresh)
                    skip = await asyncio.to_thread(repo.handled_place_ids, company.id, [pid for pid, _ in fresh],
                                                   now - timedelta(days=RECHECK_SKIPPED_DAYS))
                    _count(ctx, "already_handled", len(skip))
                    signals, outcomes = await self._visit(company, query, location,
                                                          [p for p in fresh if p[0] not in skip], reader, deadline,
                                                          ctx, now)
                    await asyncio.to_thread(repo.record_place_ids, company.id, outcomes)
                    out.extend(signals)
                    added += len(signals)
                    token = result.next_page_token
                    if not token or page == MAX_PAGES_PER_QUERY - 1:
                        complete = True  # every page of this search was read
                        break
                    if len(out) >= ctx.max_items:
                        break
                if complete:
                    await asyncio.to_thread(repo.finish_place_search, company.id, query_key(query), found, added)
        return self._finish(out, ctx)

    @staticmethod
    def _finish(out: list[RawSignal], ctx: CollectContext) -> list[RawSignal]:
        if missed := ctx.counts.get("not_checked", 0):
            ctx.warn(f"Google Maps: ran out of time; {missed} businesses were not checked. They come back on the "
                     "next search.")
        return out[: ctx.max_items]

    async def _visit(self, company: Company, query: str, location: str, places: list[tuple[str, str]],
                     reader: SiteReader, deadline: float, ctx: CollectContext,
                     now: datetime) -> tuple[list[RawSignal], dict[str, bool]]:
        """Read each business's website, SITE_CONCURRENCY at once. Returns (signals in Google's order, the Place IDs
        skipped: {place_id: False}). Added ones are recorded by services.ingest with their lead."""
        gate = asyncio.Semaphore(SITE_CONCURRENCY)
        outcomes: dict[str, bool] = {}
        results: list[RawSignal | None] = [None] * len(places)

        async def visit(index: int, place_id: str, website_uri: str) -> None:
            if not website_uri:
                ctx.count("no_website")
                outcomes[place_id] = False
                return
            async with gate:
                if _left(deadline) < MIN_SECONDS_FOR_A_SITE:
                    ctx.count("not_checked")  # not recorded: found again on the next search
                    return
                try:
                    site = await reader.read(website_uri)
                except SiteSkipped as exc:
                    ctx.count(exc.reason)
                    outcomes[place_id] = False
                    return
                except Exception:  # SiteReader only raises SiteSkipped; never let one business sink the scan
                    ctx.count("unreachable")
                    outcomes[place_id] = False
                    return
            if excluded(company, site):
                ctx.count("excluded")
                outcomes[place_id] = False
                return
            results[index] = build_signal(place_id, query, location, site, now)
            ctx.count("added")  # recorded as added by services.ingest, once the lead is stored

        await asyncio.gather(*(visit(i, pid, uri) for i, (pid, uri) in enumerate(places)))
        return [r for r in results if r is not None], outcomes
