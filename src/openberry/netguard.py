"""Outbound requests to user-supplied URLs (website auto-fill, RSS feeds, alert webhooks) may only
reach public internet addresses, so the dashboard can't be used to probe the network it runs in.

Two layers:

- `assert_public_host(url)` looks the host up once and gives a readable error. It is not enough on
  its own: the HTTP client looks the name up again when it connects, and a hostile DNS server can
  answer 127.0.0.1 the second time (DNS rebinding).
- `public_client()` is an httpx client whose connections look the host up once, refuse it unless
  every address is public, and connect to exactly the checked address. The URL, the Host header and
  TLS (SNI and certificate checks) keep the original host name. It never uses proxies from the
  environment, because a proxy would make the connection for us.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import ssl
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.ip_network("64:ff9b::/96")          # well-known NAT64 prefix: the low 32 bits are IPv4
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")  # local-use NAT64 (RFC 8215): always internal
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")        # deprecated ::a.b.c.d form


class UnsafeURL(ValueError):
    """The URL may not be fetched: not http(s), or its host is not on the public internet."""


class BlockedAddress(UnsafeURL, httpx.ConnectError):
    """A connection was refused because the host resolved to a non-public address.

    It is both an UnsafeURL (for a readable error) and an httpx.ConnectError (for generic httpx callers).
    """

    def __init__(self, message: str) -> None:
        httpx.ConnectError.__init__(self, message)


def is_public_ip(ip: IPAddress) -> bool:
    """True for globally routable unicast addresses. IPv6 forms that embed an IPv4 address
    (mapped, IPv4-compatible, 6to4, Teredo, NAT64) are only public when that IPv4 address is."""
    if ip.is_multicast or not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return ip not in _NAT64_LOCAL and all(is_public_ip(v4) for v4 in _embedded_ipv4(ip))
    return True


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    found = [v4 for v4 in (ip.ipv4_mapped, ip.sixtofour) if v4 is not None]
    if ip.teredo:
        found.extend(ip.teredo)
    if ip in _NAT64 or ip in _IPV4_COMPATIBLE:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF))
    return found


def all_public(addresses: Iterable[str]) -> bool:
    """True when there is at least one address and every one of them is public."""
    checked = False
    for address in addresses:
        try:
            if not is_public_ip(ipaddress.ip_address(address)):
                return False
        except ValueError:
            return False
        checked = True
    return checked


async def resolve(host: str, port: int | None = None) -> list[str]:
    """Every address `host` resolves to, in the resolver's preferred order (socket.gaierror if none)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def assert_public_host(url: str) -> None:
    """Early check with a readable error. public_client() checks every connection again."""
    host = urlsplit(url).hostname or ""
    try:
        addresses = await resolve(host)
    except socket.gaierror as exc:
        raise UnsafeURL(f"cannot resolve {host}") from exc
    if not all_public(addresses):
        raise UnsafeURL(f"{host} resolves to a non-public address")


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend: resolve once, refuse non-public addresses, connect to a checked one."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options: Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        try:
            with anyio.fail_after(timeout):  # the lookup and every attempt share the connect timeout
                return await self._connect(host, port, local_address, socket_options)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"connecting to {host} timed out") from exc

    async def _connect(self, host: str, port: int, local_address: str | None,
                       socket_options: Iterable[Any] | None) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await resolve(host, port)
        except socket.gaierror as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}") from exc
        if not all_public(addresses):
            raise BlockedAddress(f"{host} resolves to a non-public address")
        for address in addresses[:-1]:  # e.g. an IPv6 address on a host without IPv6: try the next one
            with contextlib.suppress(httpcore.ConnectError):
                return await self._inner.connect_tcp(address, port, local_address=local_address,
                                                     socket_options=socket_options)
        return await self._inner.connect_tcp(addresses[-1], port, local_address=local_address,
                                             socket_options=socket_options)

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    """httpx transport that only connects to public addresses. `network_backend` makes the actual
    connections once an address is checked (default: anyio; tests pass a fake)."""

    def __init__(self, *, verify: ssl.SSLContext | str | bool = True, retries: int = 0,
                 network_backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        super().__init__(verify=verify, retries=retries)
        # httpx has no option for the network backend, so give its connection pool ours.
        self._pool._network_backend = PublicOnlyBackend(network_backend)


def public_client(**kwargs: Any) -> httpx.AsyncClient:
    """An httpx.AsyncClient for user-supplied URLs (same arguments as httpx.AsyncClient)."""
    return httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False, **kwargs)
