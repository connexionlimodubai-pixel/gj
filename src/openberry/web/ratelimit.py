"""In-memory rate limits for the endpoints anonymous visitors can reach: login, public
registration and the website auto-fill it uses.

Counts live in the app's process (`app.state.limits`), which matches how OpenBerry runs: one
server process. A restart forgets them, which only gives a guesser a fresh small budget.
"""

from __future__ import annotations

import asyncio
import ipaddress
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import Request


def client_key(request: Request) -> str:
    """The client's address; IPv6 clients by their /64, since one host can use the whole network."""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host or "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class RateLimit:
    """At most `per_client` events per client and `total` events overall in a sliding `window` (seconds)."""

    def __init__(self, per_client: int, window: float, total: int | None = None) -> None:
        self.per_client, self.window, self.total = per_client, window, total
        self._events: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._lock = threading.Lock()  # sync routes run in a thread pool

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        for key in [k for k, q in self._events.items() if not q or q[-1] <= cutoff]:
            del self._events[key]
        for q in self._events.values():
            while q and q[0] <= cutoff:
                q.popleft()

    def _wait(self, key: str, now: float) -> float:
        self._prune(now)
        waits = [0.0]
        mine = self._events.get(key)
        if mine and len(mine) >= self.per_client:
            waits.append(mine[-self.per_client] + self.window - now)
        if self.total is not None and len(self._all) >= self.total:
            waits.append(self._all[-self.total] + self.window - now)
        return max(waits)

    def _record(self, key: str, now: float) -> None:
        self._events.setdefault(key, deque()).append(now)
        self._all.append(now)

    def retry_after(self, key: str, now: float | None = None) -> float:
        """Seconds until `key` may act again; 0 when it may act now."""
        with self._lock:
            return self._wait(key, time.monotonic() if now is None else now)

    def hit(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._record(key, time.monotonic() if now is None else now)

    def allow(self, key: str, now: float | None = None) -> float:
        """Record an event for `key` unless that goes over a limit; returns the seconds to wait (0 = allowed)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            wait = self._wait(key, now)
            if not wait:
                self._record(key, now)
            return wait

    def count(self, key: str | None = None, now: float | None = None) -> int:
        """Events in the window for `key`, or for everyone when `key` is None."""
        with self._lock:
            self._prune(time.monotonic() if now is None else now)
            return len(self._all if key is None else self._events.get(key, ()))

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


@dataclass
class Limits:
    """The app's limits (`app.state.limits`)."""

    # Failed logins: 5 per client per 10 minutes, then 429 until the oldest one expires.
    # Everyone's failures together also lengthen each failed attempt's delay (see auth.login).
    login_failures: RateLimit = field(default_factory=lambda: RateLimit(per_client=5, window=600))
    # Login checks run one at a time, so parallel guesses queue behind each other's delay.
    login_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Anonymous registrations and the website look-ups their form makes.
    register: RateLimit = field(default_factory=lambda: RateLimit(per_client=5, window=60, total=30))
    site_summary: RateLimit = field(default_factory=lambda: RateLimit(per_client=5, window=60, total=30))


def limits(request: Request) -> Limits:
    return request.app.state.limits


def retry_header(wait: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, int(wait + 0.999)))}
