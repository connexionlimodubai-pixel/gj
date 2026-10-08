"""Hacker News collector. (stub: to be implemented)"""

from __future__ import annotations

from ..models import Company
from .base import CollectContext, Collector, RawSignal


class HackerNewsCollector(Collector):
    name = "hackernews"
    label = "Hacker News"
    signal_types = ('competitor_engagement', 'keyword_mention', 'hiring')
    requires = "keywords or competitors"

    def is_configured(self, company: Company) -> bool:
        return False

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        return []
