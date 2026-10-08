"""SEC EDGAR collector: Form D funding filings and 8-K executive changes. (stub: to be implemented)"""

from __future__ import annotations

from ..models import Company
from .base import CollectContext, Collector, RawSignal


class SecEdgarCollector(Collector):
    name = "sec_edgar"
    label = "SEC EDGAR (US funding & exec changes)"
    signal_types = ("funding", "job_change")
    requires = "sec_queries"

    def is_configured(self, company: Company) -> bool:
        return False

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        return []
