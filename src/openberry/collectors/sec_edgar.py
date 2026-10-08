"""SEC EDGAR collector: new Form D funding filings and 8-K Item 5.02 leadership changes.

Source (no key, no signup)
    GET https://efts.sec.gov/LATEST/search-index
        ?q="<query>"&forms=D|8-K&dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD[&from=N]
    This is EDGAR full-text search (EFTS), the backend of https://www.sec.gov/edgar/search. SEC does
    not document it as an API, so every field is read defensively and an unexpected shape is reported
    as a warning, never an exception. It answers Elasticsearch-style JSON:
    {"hits": {"total": {"value": n, "relation": "eq"|"gte"}, "hits": [{"_id": "<adsh>:<file>",
    "_source": {"ciks", "display_names", "adsh", "form", "root_forms", "file_type", "file_date",
    "items", "biz_locations", "inc_states", "sics", "period_ending", ...}}]}}, about 100 hits per page
    (paged with `from`), ranked by relevance rather than date. Hits are per DOCUMENT, so one filing can
    come back several times (8-K main document + exhibits): filings are de-duplicated on `adsh`.
    Each configured query is sent as a quoted phrase (a query that already contains double quotes,
    e.g. '"freight audit" OR "fleet telematics"', is sent unchanged). Hits whose form is not the one
    asked for are ignored (and reported), so a changed or ignored `forms` filter cannot turn a 10-K
    into a "funding" signal.
    What a query can match differs by form: an 8-K and its exhibits (press releases) are prose, so
    topical phrases ("logistics software") work. A Form D is structured data only (issuer name and
    address, related persons, industry group such as "Other Technology" or "Computers", amounts), so
    a topical phrase rarely matches one; company names, people, cities or industry-group labels do.

What it emits (both account-level: lead=None, account=<filer name>; no domain is known)
    funding     Form D: notice of an exempt private offering, filed within 15 days of the first sale,
                i.e. most US startup priced rounds. Strength 60 (a fresh round is an above-typical
                buying window). Skipped: amendments (form/root_forms/file_type "D/A": not a new round)
                and pooled investment vehicles, which file most Form Ds but are not buyers. Fund
                detection is conservative because the hit has no industry group (that is only in the
                filing's XML, which we do not fetch one by one):
                  - an Investment Company Act 3(c)(1) or 3(c)(7) exclusion in the hit's `items`
                    ("3C.1" / "3C.7": private funds), or
                  - a vehicle-like name: "Fund(s)", "L.P."/"LP", "SPV", "a Series of ..." or a name
                    ending in "Series" ("Capria Opportunities, LP - Eduvanz Series"), "Capital /
                    Venture / Equity / Investment / Growth / Opportunity Partners", "Co-Invest",
                    "Feeder". Operating companies whose legal name contains one of these are lost.
    job_change  8-K whose filing-level `items` include "5.02" (departure, election or appointment of
                directors or certain officers) at an SEC-reporting, mostly public, company. Strength 55.
                8-K/A amendments are skipped (they complete an event that was already reported). The
                people's names are only in the document text: the summary points Claude to the filing.
    Filer names come from display_names[0] with the "(CIK ...)" and ticker "(ABC, ABCW)" suffixes and
    EDGAR state tags such as "/DE/" or "/MN" removed; tickers go to raw["tickers"]. The CIK is the one
    in that same display name (else ciks[0]), so name, CIK and url agree for co-registrant filings.
    Summaries show US state codes only: EDGAR's foreign codes ("E9" = Cayman Islands, "X0") stay in
    raw. url is the filing's archive
    folder https://www.sec.gov/Archives/edgar/data/<cik>/<adsh without dashes>/ and external_id is
    "sec_edgar:D:<adsh>" or "sec_edgar:8-K:<adsh>". A filing matched by several queries is returned
    once, with every matching query in raw["queries"].

Time
    EDGAR only gives a filing DATE. occurred_at is that date at 00:00 UTC, and a filing counts as newer
    than ctx.since when it was filed on or after since's UTC calendar day: the same inclusive day that
    `startdt` asks the server for. Re-reporting is harmless (external ids are stable) and comparing
    clock times would drop filings made later on since's day.

Limits and politeness
    SEC's fair-access policy: at most 10 requests/second per user, and every request must declare a
    User-Agent with a contact e-mail ("OpenBerry <email>", from OPENBERRY_CONTACT_EMAIL or else the
    company's contact e-mail; it must be a plain ASCII address because it goes in an HTTP header;
    without a usable one nothing is fetched). Undeclared tools get HTTP 403.
    Requests are sequential, `request_interval` (0.2 s) apart. At most MAX_QUERIES queries per scan
    (rotated daily when more are configured), one search per query and signal type (Form D only when
    "funding" is enabled, 8-K only for "job_change"), a second page only while requests remain for
    every search still to run, MAX_REQUESTS_PER_SCAN requests in total, MAX_FILINGS_PER_SEARCH filings
    per search and ctx.max_items signals overall. A 429 or 403 stops the scan's SEC requests (the
    limit is per IP across all of sec.gov), as do three failed requests in a row or the time budget.

Coverage and terms
    US only: Form D covers issuers selling under Regulation D (US offerings), and 8-Ks come from
    SEC-reporting companies. Full-text search covers 2001 onward with a short indexing lag. EDGAR data
    is public; SEC asks automated users to declare themselves, stay under 10 requests/second and to
    download only what they need, which this collector does.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ..config import Settings
from ..models import Company, SignalIn
from .base import CollectContext, Collector, RawSignal, parse_time, strip_html, truncate

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/"
SOURCE = "sec_edgar"

MAX_QUERIES = 5
MAX_FILINGS_PER_SEARCH = 40
MAX_PAGES_PER_SEARCH = 2
MAX_REQUESTS_PER_SCAN = 15        # 5 queries x 2 forms, plus a few second pages
MAX_FAILURES_IN_A_ROW = 3
FORM_D_STRENGTH = 60
EXEC_CHANGE_STRENGTH = 55
TITLE_LIMIT = 160
SUMMARY_LIMIT = 500

NO_EMAIL_WARNING = "SEC EDGAR needs a contact e-mail: set OPENBERRY_CONTACT_EMAIL or the company contact e-mail"
BAD_EMAIL_WARNING = ("SEC EDGAR: the contact e-mail is not a plain address that can be sent in the User-Agent "
                     "header (ASCII only, like ops@example.com): fix OPENBERRY_CONTACT_EMAIL or the company "
                     "contact e-mail")

_EMAIL_RE = re.compile(r"[A-Za-z0-9!#$%&*+/=?^_`{|}~.\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")
_ADSH_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
_CIK_SUFFIX_RE = re.compile(r"\s*\(\s*CIK\s*#?\s*(\d{1,10})\s*\)\s*$", re.I)
_TICKERS_SUFFIX_RE = re.compile(r"\s*\(\s*([A-Z0-9][A-Z0-9.\-]{0,9}(?:\s*,\s*[A-Z0-9][A-Z0-9.\-]{0,9})*)\s*\)\s*$")
_STATE_TAG_RE = re.compile(r"(?:\s*/[A-Z]{2,4}/?)+\s*$")  # EDGAR name tags: "ACME CORP /DE/", "X CO/MN", "/DE/ /NEW/"
_US_STATE_RE = re.compile(r"[A-Z]{2}")                        # EDGAR's non-US codes all contain a digit ("E9", "X0", "A6")
_FOREIGN_CODE_SUFFIX_RE = re.compile(r",\s*(?=[A-Z0-9]{2}$)[A-Z]*\d[A-Z0-9]*$")   # "London, X0" -> "London"
_FUND_NAME_RE = re.compile(
    r"\bfunds?\b"
    r"|\bL\.?\s?P\b\.?(?![a-z0-9])"
    r"|\bSPVs?\b"
    r"|\ba series of\b"
    r"|\bseries(?:\s+(?:[ivx]+|\d+|[a-z]))?\s*$"
    r"|\b(?:capital|venture|ventures|equity|investment|growth|opportunity|opportunities) partners\b"
    r"|\bco-?invest(?:ment|ors?)?\b"
    r"|\bfeeder\b",
    re.I,
)
_FUND_EXCLUSIONS = {"3C.1", "3C.7"}   # Investment Company Act 3(c)(1) / 3(c)(7): private funds
_EXEMPTIONS = {"06B": "Rule 506(b)", "06C": "Rule 506(c)"}


@dataclass(frozen=True)
class FormSearch:
    """One kind of EDGAR search: which form to ask for and what signal it becomes."""

    form: str            # value of the `forms` parameter, also used in external ids
    signal_type: str
    label: str           # for warnings


FORM_D = FormSearch("D", "funding", "Form D")
FORM_8K = FormSearch("8-K", "job_change", "8-K")


class SecEdgarCollector(Collector):
    name = "sec_edgar"
    label = "SEC EDGAR (US funding & exec changes)"
    signal_types = ("funding", "job_change")
    requires = "sec_queries (and a contact e-mail: OPENBERRY_CONTACT_EMAIL or the company contact e-mail)"

    # Seconds between two requests (SEC allows 10/s; we stay far below), and seconds after which no
    # new request starts (kept below services.COLLECTOR_TIMEOUT_SECONDS). Tests set the interval to 0.
    request_interval: float = 0.2
    time_budget: float = 90.0

    def is_configured(self, company: Company) -> bool:
        return bool(company.signals.sec_queries)

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        forms = enabled_forms(company)
        if not forms or not company.signals.sec_queries or ctx.max_items <= 0:
            return []
        email = contact_email(ctx.settings, company)
        if not email:
            configured = any((v or "").strip() for v in (ctx.settings.contact_email, company.contact_email))
            ctx.warn(BAD_EMAIL_WARNING if configured else NO_EMAIL_WARNING)
            return []

        queries = select_queries(company.signals.sec_queries, ctx)
        searches = [(query, form) for query in queries for form in forms]
        today = _utcnow().date()
        since_day = min(_utc(ctx.since).date(), today)
        fetcher = _Fetcher(ctx, f"OpenBerry {email}", self.request_interval, self.time_budget)
        found: dict[str, Filing] = {}     # adsh -> filing, in discovery order

        for index, (query, form) in enumerate(searches):
            left = len(searches) - index
            if fetcher.stopped:
                break
            if len(found) >= ctx.max_items:
                ctx.warn(f"SEC EDGAR: reached the limit of {ctx.max_items} signals; "
                         f"{left} search(es) not run this scan")
                break
            if (reason := fetcher.out_of_budget()) is not None:
                ctx.warn(f"SEC EDGAR: {reason}; {left} search(es) not run this scan")
                break
            await self._run_search(fetcher, query, form, since_day, today, left - 1, found, ctx)

        return [build_signal(f) for f in list(found.values())[: ctx.max_items]]

    async def _run_search(self, fetcher: _Fetcher, query: str, form: FormSearch, since_day: date,
                          today: date, searches_after: int, found: dict[str, Filing],
                          ctx: CollectContext) -> None:
        """Fetch up to MAX_PAGES_PER_SEARCH pages of one search and add new matching filings to `found`."""
        offset = kept = malformed = other_forms = 0
        for page in range(MAX_PAGES_PER_SEARCH):
            # A further page only when a request is still left for every search that has not run yet.
            if page and (fetcher.used + searches_after >= fetcher.max_requests or fetcher.out_of_budget()):
                break
            params = search_params(query, form, since_day, today, offset)
            result = await fetcher.search(params, f'search "{query}" ({form.label})')
            if result is None:
                break
            hits, total = result
            for hit in hits:
                try:
                    filing = parse_hit(hit, form, query)
                except WrongForm:
                    other_forms += 1
                    continue
                except (ValueError, TypeError, AttributeError):
                    malformed += 1
                    continue
                if filing is None or filing.file_date.date() < since_day:
                    continue
                if filing.adsh in found:
                    if query not in found[filing.adsh].queries:
                        found[filing.adsh].queries.append(query)
                    continue
                found[filing.adsh] = filing
                kept += 1
                if kept >= MAX_FILINGS_PER_SEARCH or len(found) >= ctx.max_items:
                    break
            offset += len(hits)
            if (not hits or offset >= total or kept >= MAX_FILINGS_PER_SEARCH
                    or len(found) >= ctx.max_items):
                break
        if malformed:
            ctx.warn(f'SEC EDGAR search "{query}" ({form.label}): skipped {malformed} malformed hit(s)')
        if other_forms:
            ctx.warn(f'SEC EDGAR search "{query}" ({form.label}): ignored {other_forms} hit(s) of other forms '
                     "(the search API may have changed)")


# --------------------------------------------------------------------------------------
# Configuration helpers
# --------------------------------------------------------------------------------------


def enabled_forms(company: Company) -> list[FormSearch]:
    enabled = set(company.signals.enabled_types)
    return [f for f in (FORM_D, FORM_8K) if f.signal_type in enabled]


def contact_email(settings: Settings, company: Company) -> str:
    """The first usable e-mail among Settings.contact_email and the company's contact e-mail."""
    for candidate in (settings.contact_email, company.contact_email):
        value = (candidate or "").strip()
        if _EMAIL_RE.fullmatch(value):
            return value
    return ""


def select_queries(queries: list[str], ctx: CollectContext) -> list[str]:
    """Unique non-empty queries, at most MAX_QUERIES, rotated daily when more are configured."""
    by_key: dict[str, str] = {}
    for query in queries:
        if query.strip():
            by_key.setdefault(query.strip().lower(), query.strip())
    unique = list(by_key.values())
    if len(unique) <= MAX_QUERIES:
        return unique
    start = _utc(ctx.since).date().toordinal() % len(unique)
    ctx.warn(f"SEC EDGAR: {len(unique)} queries configured but only {MAX_QUERIES} are searched per scan; "
             "the searched set rotates daily")
    return (unique[start:] + unique[:start])[:MAX_QUERIES]


def phrase(query: str) -> str:
    """Quote the query as an exact phrase unless the user already wrote quotes (boolean queries)."""
    query = query.strip()
    return query if '"' in query else f'"{query}"'


def search_params(query: str, form: FormSearch, since_day: date, today: date, offset: int = 0) -> dict[str, str]:
    params = {"q": phrase(query), "forms": form.form, "dateRange": "custom",
              "startdt": since_day.isoformat(), "enddt": today.isoformat()}
    if offset:
        params["from"] = str(offset)
    return params


# --------------------------------------------------------------------------------------
# Hits -> filings
# --------------------------------------------------------------------------------------


@dataclass
class Filing:
    form: FormSearch
    adsh: str
    cik: str                      # without leading zeros
    entity: str
    file_date: datetime
    tickers: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    biz_locations: list[str] = field(default_factory=list)
    inc_states: list[str] = field(default_factory=list)
    sics: list[str] = field(default_factory=list)
    period_ending: str = ""
    queries: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(cik=self.cik, folder=self.adsh.replace("-", ""))


class WrongForm(Exception):
    """The hit is a filing of another form than the one searched for."""


def parse_hit(hit: Any, form: FormSearch, query: str) -> Filing | None:
    """A Filing for a hit we keep, None for one we skip on purpose; ValueError when malformed,
    WrongForm when the hit is not the form we asked for."""
    if not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict):
        raise ValueError("hit without _source")
    src: dict[str, Any] = hit["_source"]
    if is_amendment(src):
        return None
    filed_as = {f.upper() for f in (_s(src.get("form")), *_strs(src.get("root_forms"))) if f}
    if filed_as and form.form.upper() not in filed_as:
        raise WrongForm(", ".join(sorted(filed_as)))
    items = [i.upper() for i in _strs(src.get("items"))]
    if form is FORM_8K and "5.02" not in items:
        return None

    adsh = _s(src.get("adsh")) or _s(hit.get("_id")).split(":", 1)[0]
    if not _ADSH_RE.fullmatch(adsh):
        raise ValueError("hit without accession number")
    names = _strs(src.get("display_names"))
    entity, tickers, name_cik = split_display_name(names[0] if names else "")
    # The CIK printed in the name we use, so name, CIK and url agree when a filing has co-registrants.
    ciks = [c for c in _strs(src.get("ciks")) if c.isdigit()]
    cik = str(int(name_cik or (ciks[0] if ciks else "0")))
    if not entity or cik == "0":
        raise ValueError("hit without filer name or CIK")
    filed = parse_time(_s(src.get("file_date")))
    if filed is None:
        raise ValueError("hit without file_date")
    if form is FORM_D and is_pooled_fund(entity, items):
        return None
    return Filing(
        form=form,
        adsh=adsh,
        cik=cik,
        entity=entity,
        file_date=filed.astimezone(timezone.utc),
        tickers=tickers,
        items=items,
        biz_locations=_strs(src.get("biz_locations")),
        inc_states=_strs(src.get("inc_states")),
        sics=_strs(src.get("sics")),
        period_ending=_s(src.get("period_ending")),
        queries=[query],
    )


def is_amendment(src: dict[str, Any]) -> bool:
    forms = [_s(src.get("form")), _s(src.get("file_type")), *_strs(src.get("root_forms"))]
    return any(f.upper().endswith("/A") for f in forms)


def is_pooled_fund(entity: str, items: list[str]) -> bool:
    """Conservative guess that a Form D filer is an investment vehicle rather than an operating company."""
    return bool(_FUND_EXCLUSIONS.intersection(items)) or bool(_FUND_NAME_RE.search(entity))


def split_display_name(display: str) -> tuple[str, list[str], str]:
    """'Zapata Computing Holdings Inc. (ZPTA, ZPTAW) (CIK 0001843714)' -> (name, tickers, cik)."""
    text = strip_html(display)     # plain text in practice; also collapses the double spaces
    cik = ""
    if m := _CIK_SUFFIX_RE.search(text):
        cik, text = m.group(1), text[: m.start()]
    tickers: list[str] = []
    if m := _TICKERS_SUFFIX_RE.search(text):
        tickers, text = [t.strip() for t in m.group(1).split(",")], text[: m.start()]
    text = _STATE_TAG_RE.sub("", text).strip(" ,;-")
    return text, tickers, cik


# --------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------


def build_signal(f: Filing) -> RawSignal:
    if f.form is FORM_D:
        title = f"Form D filing: {f.entity}"
        strength = FORM_D_STRENGTH
        raw: dict[str, Any] = {"exemptions": f.items}
    else:
        title = f"Leadership change (8-K 5.02): {f.entity}"
        strength = EXEC_CHANGE_STRENGTH
        raw = {"items": f.items, "tickers": f.tickers, "period_ending": f.period_ending, "sics": f.sics}
    raw = {"form": f.form.form, "adsh": f.adsh, "cik": f.cik, "biz_locations": f.biz_locations,
           "inc_states": f.inc_states, **raw, "queries": f.queries}
    signal = SignalIn(
        type=f.form.signal_type,
        title=truncate(title, TITLE_LIMIT),
        summary=build_summary(f),
        url=f.url,
        source=SOURCE,
        external_id=f"{SOURCE}:{f.form.form}:{f.adsh}",
        strength=strength,
        occurred_at=f.file_date,
        raw={k: v for k, v in raw.items() if v not in (None, "", [], {})},
    )
    places = [p for p in (_FOREIGN_CODE_SUFFIX_RE.sub("", loc).strip() for loc in f.biz_locations) if p]
    return RawSignal(signal=signal, account=f.entity, account_location=places[0] if places else "")


def build_summary(f: Filing) -> str:
    day = f.file_date.date().isoformat()
    where = []
    places = [p for p in (_FOREIGN_CODE_SUFFIX_RE.sub("", loc).strip() for loc in f.biz_locations) if p]
    if places:
        where.append(f"Based in {'; '.join(places[:2])}")
    states = [s for s in f.inc_states if _US_STATE_RE.fullmatch(s)]
    if states:
        where.append(f"incorporated in {', '.join(states[:2])}")
    matched = ", ".join(phrase(q) for q in f.queries)
    if f.form is FORM_D:
        rules = [label for code, label in _EXEMPTIONS.items() if code in f.items]
        text = (f"{f.entity} filed a Form D on {day}: notice of an exempt private securities offering, "
                f"usually a new funding round{' under ' + ' and '.join(rules) if rules else ''}.")
        tail = "The filing lists the amount raised and the executive officers and directors."
    else:
        name = f"{f.entity} ({', '.join(f.tickers)})" if f.tickers else f.entity
        others = [i for i in f.items if i != "5.02"]
        text = (f"{name} filed an 8-K on {day} reporting Item 5.02: departure, election or appointment "
                "of directors or certain officers.")
        if others:
            text += f" Also reported: Item {', '.join(others)}."
        tail = "The people involved are named in the filing text."
    if where:
        text += f" {', '.join(where)}."
    text += f" Matched SEC full-text search {matched}. {tail}"
    return truncate(text, SUMMARY_LIMIT)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class _Fetcher:
    """Sequential, paced, capped EFTS requests. Never raises; warns and stops instead."""

    def __init__(self, ctx: CollectContext, user_agent: str, interval: float, time_budget: float) -> None:
        self.ctx = ctx
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.interval = interval
        self.deadline = time.monotonic() + time_budget
        self.max_requests = MAX_REQUESTS_PER_SCAN
        self.used = 0
        self.failures = 0
        self.stopped = False

    def out_of_budget(self) -> str | None:
        if self.used >= self.max_requests:
            return f"request cap reached ({self.max_requests} per scan)"
        if time.monotonic() >= self.deadline:
            return "time budget for this scan used up"
        return None

    async def search(self, params: dict[str, str], what: str) -> tuple[list[Any], int] | None:
        """(hits, total hit count) for one page, or None when the request failed (already warned)."""
        if self.stopped or self.out_of_budget() is not None:
            return None
        if self.used and self.interval > 0:
            await _sleep(self.interval)
        self.used += 1
        try:
            resp = await self.ctx.client.get(EFTS_URL, params=params, headers=self.headers)
        except httpx.TimeoutException:
            self._fail(f"SEC EDGAR {what}: request timed out")
            return None
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:   # ValueError: e.g. unencodable header
            self._fail(f"SEC EDGAR {what}: request failed ({type(exc).__name__})")
            return None

        status = resp.status_code
        if status == 429:
            retry = resp.headers.get("retry-after", "").strip()
            hint = f", retry after {truncate(retry, 40)}" if retry else ""
            self._stop(f"SEC EDGAR rate limit hit (HTTP 429{hint}); no more SEC requests this scan")
            return None
        if status == 403:
            self._stop("SEC EDGAR refused access (HTTP 403). SEC blocks undeclared or too-fast "
                       "automated tools: check the contact e-mail and scan less often; "
                       "no more SEC requests this scan")
            return None
        if not resp.is_success:
            self._fail(f"SEC EDGAR {what}: HTTP {status}")
            return None
        try:
            page = _hits(resp.json())
        except ValueError:
            self._fail(f"SEC EDGAR {what}: response was not valid JSON")
            return None
        if page is None:
            self._fail(f"SEC EDGAR {what}: unexpected response format")
            return None
        self.failures = 0
        if resp.headers.get("x-ratelimit-remaining", "").strip() == "0":
            self._stop("SEC EDGAR: rate-limit budget exhausted; no more SEC requests this scan")
        return page

    def _fail(self, message: str) -> None:
        self.ctx.warn(message)
        self.failures += 1
        if self.failures >= MAX_FAILURES_IN_A_ROW and not self.stopped:
            self._stop(f"SEC EDGAR: {self.failures} failed requests in a row; no more SEC requests this scan")

    def _stop(self, message: str) -> None:
        self.stopped = True
        self.ctx.warn(message)


def _hits(data: Any) -> tuple[list[Any], int] | None:
    """The hit list and total from an EFTS response, or None when the shape is not the expected one."""
    outer = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(outer, dict) or not isinstance(outer.get("hits"), list):
        return None
    total = outer.get("total")
    value = total.get("value") if isinstance(total, dict) else total
    count = value if isinstance(value, int) and not isinstance(value, bool) else len(outer["hits"])
    return outer["hits"], count


# --------------------------------------------------------------------------------------
# Payload helpers (fields can be missing, null or of an unexpected type)
# --------------------------------------------------------------------------------------


def _s(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _strs(value: Any) -> list[str]:
    """Non-empty strings from a list (or a single scalar), order kept, duplicates dropped."""
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(s for s in (_s(v) for v in values) if s))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _utcnow() -> datetime:
    """Current time; a function so tests can pin "today" (the search's enddt)."""
    return datetime.now(timezone.utc)


_sleep = asyncio.sleep   # module-level so tests can record the pacing without patching asyncio
