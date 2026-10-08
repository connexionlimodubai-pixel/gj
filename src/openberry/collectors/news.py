"""News collectors: Google News RSS search and generic RSS/Atom feeds. (stub: to be implemented)"""

from __future__ import annotations

from ..models import Company
from .base import CollectContext, Collector, RawSignal


class GoogleNewsCollector(Collector):
    name = "google_news"
    label = "Google News"
    signal_types = ("funding", "company_news", "job_change")
    requires = "news_queries"

    def is_configured(self, company: Company) -> bool:
        return False

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        return []


class RssCollector(Collector):
    name = "rss"
    label = "RSS / Atom feeds"
    signal_types = ("funding", "company_news", "keyword_mention")
    requires = "rss_feeds"

    def is_configured(self, company: Company) -> bool:
        return False

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        return []
