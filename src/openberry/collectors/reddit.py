"""Reddit collector. (stub: to be implemented)"""

from __future__ import annotations

from ..models import Company
from .base import CollectContext, Collector, RawSignal


class RedditCollector(Collector):
    name = "reddit"
    label = "Reddit"
    signal_types = ('competitor_engagement', 'keyword_mention')
    requires = "keywords or competitors (optionally subreddits)"

    def is_configured(self, company: Company) -> bool:
        return False

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        return []
