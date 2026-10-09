"""Signal collectors for free public data sources.

Add a new source by subclassing `Collector` in a module here and listing it in ALL.
"""

from __future__ import annotations

from .base import CollectContext, Collector, RawSignal
from .github import GitHubCollector
from .hackernews import HackerNewsCollector
from .jobs import JobBoardsCollector
from .news import GoogleNewsCollector, RssCollector
from .reddit import RedditCollector
from .sec_edgar import SecEdgarCollector

ALL: list[Collector] = [
    HackerNewsCollector(),
    RedditCollector(),
    GitHubCollector(),
    JobBoardsCollector(),
    GoogleNewsCollector(),
    RssCollector(),
    SecEdgarCollector(),
]

COLLECTORS: dict[str, Collector] = {c.name: c for c in ALL}


def get_collectors(names: list[str] | None = None) -> list[Collector]:
    if not names:
        return list(ALL)
    unknown = [n for n in names if n not in COLLECTORS]
    if unknown:
        raise ValueError(f"unknown collector(s): {', '.join(unknown)}; available: {', '.join(COLLECTORS)}")
    return [COLLECTORS[n] for n in names]


__all__ = ["ALL", "COLLECTORS", "CollectContext", "Collector", "RawSignal", "get_collectors"]
