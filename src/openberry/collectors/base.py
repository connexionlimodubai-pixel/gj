"""Collector interface. A collector turns one free public data source into RawSignals."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

import httpx

from ..config import Settings
from ..models import Company, LeadIn, SignalIn


@dataclass
class RawSignal:
    """One observation from a source.

    - `lead` set: a person did something (posted, starred, commented) -> person lead.
    - `lead` None and `account` set: something happened at a company (hiring, funding,
      news) -> account-level lead that people at that company inherit intent from.
    """

    signal: SignalIn
    lead: LeadIn | None = None
    account: str = ""
    account_domain: str = ""
    account_location: str = ""


@dataclass
class CollectContext:
    client: httpx.AsyncClient
    since: datetime
    settings: Settings
    warnings: list[str] = field(default_factory=list)
    max_items: int = 200  # per collector per scan, keeps us polite to free APIs
    # What happened, for the scan stats ("businesses": 40, "no_website": 6...). Not problems: those are warnings.
    counts: dict[str, int] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def count(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


class Collector(ABC):
    name: ClassVar[str]                       # unique key, e.g. "hackernews"
    label: ClassVar[str]                      # human label for the dashboard
    signal_types: ClassVar[tuple[str, ...]]   # types this collector can emit
    requires: ClassVar[str]                   # what the company must configure, shown in the UI
    # services.run_scan cancels collect() after this many seconds, losing what it found. None: the default (120).
    timeout_seconds: ClassVar[float | None] = None

    @abstractmethod
    def is_configured(self, company: Company) -> bool:
        """True when the company profile has the inputs this collector needs."""

    @abstractmethod
    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        """Fetch and return signals newer than ctx.since. Must not raise for a single bad item."""

    def enabled_for(self, company: Company) -> bool:
        enabled = set(company.signals.enabled_types)
        return self.is_configured(company) and any(t in enabled for t in self.signal_types)

    def usage(self) -> dict[str, Any] | None:
        """Paid-API usage to show with the collector (dashboard Sources card, get_company_profile), or None."""
        return None


# --------------------------------------------------------------------------------------
# Helpers shared by collectors
# --------------------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    import html

    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def find_terms(text: str, terms: list[str]) -> list[str]:
    """Case-insensitive whole-word/phrase matches of `terms` in `text` (in the order given)."""
    hay = (text or "").lower()
    hits = []
    for term in terms:
        t = term.strip().lower()
        if t and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", hay):
            hits.append(term)
    return hits


def parse_time(value: Any) -> datetime | None:
    """Parse ISO strings, unix timestamps (s or ms) and RFC 2822 dates to aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def truncate(text: str, limit: int = 280) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
