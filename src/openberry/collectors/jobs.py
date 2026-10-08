"""Job boards (Greenhouse / Lever / Ashby) collector. (stub: to be implemented)"""

from __future__ import annotations

from ..models import Company
from .base import CollectContext, Collector, RawSignal


class JobBoardsCollector(Collector):
    name = "jobs"
    label = "Job boards (Greenhouse / Lever / Ashby)"
    signal_types = ('hiring',)
    requires = "job_boards"

    def is_configured(self, company: Company) -> bool:
        return False

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        return []
