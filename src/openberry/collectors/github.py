"""GitHub stargazers collector. (stub: to be implemented)"""

from __future__ import annotations

from ..models import Company
from .base import CollectContext, Collector, RawSignal


class GitHubCollector(Collector):
    name = "github"
    label = "GitHub stargazers"
    signal_types = ('github_star',)
    requires = "github_repos"

    def is_configured(self, company: Company) -> bool:
        return False

    async def collect(self, company: Company, ctx: CollectContext) -> list[RawSignal]:
        return []
