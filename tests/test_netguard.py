"""Outbound requests to user-supplied URLs only reach public addresses, even when DNS changes its answer."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpcore
import httpx
import pytest

from openberry import netguard, notify, repo, website
from openberry.collectors import news
from openberry.collectors.base import CollectContext
from openberry.config import get_settings
from openberry.models import Company, LeadIn

PUBLIC_IP = "93.184.216.34"


# --------------------------------------------------------------------------------------
# Which addresses count as public
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "224.0.0.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1",
    "64:ff9b::a9fe:a9fe",            # NAT64 of 169.254.169.254 (is_global says True)
    "64:ff9b::a00:1",                # NAT64 of 10.0.0.1 (is_global says True)
    "64:ff9b:1::808:808",            # local-use NAT64 prefix
    "::a00:1",                       # IPv4-compatible 10.0.0.1 (is_global says True)
    "::ffff:10.0.0.1",               # IPv4-mapped
    "2002:a9fe:a9fe::1",             # 6to4 of 169.254.169.254
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
])
def test_non_public_addresses_are_refused(address):
    assert not netguard.all_public([address])


@pytest.mark.parametrize("address", [PUBLIC_IP, "8.8.8.8", "2001:4860:4860::8888", "64:ff9b::808:808",
                                     "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address):
    assert netguard.all_public([address])


def test_one_private_answer_or_none_at_all_is_refused():
    assert not netguard.all_public([PUBLIC_IP, "127.0.0.1"])
    assert not netguard.all_public([])
    assert not netguard.all_public(["not-an-ip"])


async def test_nat64_address_of_a_private_host_is_refused(monkeypatch):
    fake_dns(monkeypatch, {"nat64.test": ["64:ff9b::a9fe:a9fe"]})
    with pytest.raises(netguard.UnsafeURL, match="non-public"):
        await website.assert_public_host("http://nat64.test/latest/meta-data/")
    with pytest.raises(netguard.UnsafeURL, match="non-public"):
        await website.assert_public_host("http://[64:ff9b::a00:1]/")


# --------------------------------------------------------------------------------------
# DNS rebinding: the first lookup says public, the connection would go to 127.0.0.1
# --------------------------------------------------------------------------------------


def fake_dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str]]) -> list[str]:
    """Answer lookups for the given names from `answers`, one entry ("ip" or "ip,ip") per lookup with
    the last one repeating, like a zero-TTL rebinding DNS server. Returns the answers handed out."""
    real = socket.getaddrinfo
    handed_out: list[str] = []

    def getaddrinfo(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0):
        name = host.decode() if isinstance(host, bytes) else host
        if name not in answers:
            return real(host, port, family, type, proto, flags)
        queue = answers[name]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        handed_out.append(answer)
        return [_addrinfo(ip, int(port or 0)) for ip in answer.split(",")]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return handed_out


def _addrinfo(ip: str, port: int) -> tuple[Any, ...]:
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))


class InternalServer:
    """An 'internal' HTTP service on 127.0.0.1 that records every connection it gets."""

    PAGE = (b"<html><head><title>INTERNAL ONLY</title><meta name='description' content='secret=xyz'></head>"
            b"<body><h1>Admin</h1></body></html>")

    def __init__(self) -> None:
        self.connections = 0
        self.port = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         b"Content-Length: %d\r\n\r\n%s" % (len(self.PAGE), self.PAGE))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def internal() -> AsyncIterator[InternalServer]:
    server = InternalServer()
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    server.port = listener.sockets[0].getsockname()[1]
    async with listener:
        yield server


@pytest.fixture
def rebinding_dns(monkeypatch) -> list[str]:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)  # connect directly, as a server without a proxy would
    return fake_dns(monkeypatch, {"rebind.test": [PUBLIC_IP, "127.0.0.1"]})


async def test_site_summary_refuses_a_host_that_rebinds_to_loopback(internal, rebinding_dns):
    with pytest.raises(website.UnsafeURL, match="rebind.test resolves to a non-public address"):
        await website.fetch_site_summary(f"http://rebind.test:{internal.port}/")
    assert rebinding_dns == [PUBLIC_IP, "127.0.0.1"]
    assert internal.connections == 0


async def test_webhooks_refuse_a_host_that_rebinds_to_loopback(internal, rebinding_dns, company, hot_lead, caplog):
    hooked = with_webhook(company, f"https://rebind.test:{internal.port}/services/T0/B0/token")
    with caplog.at_level(logging.WARNING, logger="openberry.notify"):
        assert await notify.notify_hot_leads(hooked, [hot_lead]) == []
    assert internal.connections == 0
    assert "rebind.test resolves to a non-public address" in caplog.text and "token" not in caplog.text


async def test_rss_feeds_refuse_a_host_that_rebinds_to_loopback(internal, rebinding_dns, company):
    feed = f"http://rebind.test:{internal.port}/feed"
    company = company.model_copy(update={"signals": company.signals.model_copy(update={"rss_feeds": [feed]})})
    async with httpx.AsyncClient(trust_env=False) as client:  # the scan's shared client
        ctx = CollectContext(client=client, since=datetime(2026, 9, 1, tzinfo=timezone.utc), settings=get_settings())
        collector = news.RssCollector()
        collector.request_interval = 0
        assert await collector.collect(company, ctx) == []
    assert internal.connections == 0
    assert ctx.warnings == [f"RSS: feed {news.feed_label(feed)} skipped: rebind.test resolves to a "
                            "non-public address (only public hosts are fetched)"]


@pytest.mark.parametrize("answer", ["127.0.0.1", f"{PUBLIC_IP},127.0.0.1"])
async def test_public_client_refuses_private_answers_at_connect(monkeypatch, internal, answer):
    fake_dns(monkeypatch, {"internal.test": [answer]})
    async with netguard.public_client() as client:
        with pytest.raises(netguard.BlockedAddress) as caught:
            await client.get(f"http://internal.test:{internal.port}/")
    assert isinstance(caught.value, httpx.ConnectError)  # generic httpx callers see a connect error
    assert internal.connections == 0


# --------------------------------------------------------------------------------------
# The guarded transport still speaks normal HTTPS to the host name
# --------------------------------------------------------------------------------------


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.response = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.record.setdefault("sent", b"")
        self.record["sent"] += buffer

    async def aclose(self) -> None:
        pass

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        self.record["tls"] = (server_hostname, ssl_context.check_hostname, ssl_context.verify_mode)
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class FakeBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, refuse: frozenset[str] = frozenset()) -> None:
        self.record: dict[str, Any] = {"connects": []}
        self.refuse = refuse

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options: Any = None) -> FakeStream:
        self.record["connects"].append((host, port))
        if host in self.refuse:
            raise httpcore.ConnectError("network unreachable")
        return FakeStream(self.record)

    async def sleep(self, seconds: float) -> None:
        pass


async def test_https_connects_to_the_checked_ip_but_verifies_the_host_name(monkeypatch):
    fake_dns(monkeypatch, {"public.test": [f"2001:4860:4860::8888,{PUBLIC_IP}"]})
    backend = FakeBackend(refuse=frozenset({"2001:4860:4860::8888"}))  # e.g. no IPv6 route: try the next one
    transport = netguard.PublicOnlyTransport(network_backend=backend)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://public.test/path?q=1")
    assert resp.status_code == 200 and resp.text == "ok"
    # One lookup, then the checked addresses in order: no second lookup by the HTTP stack.
    assert backend.record["connects"] == [("2001:4860:4860::8888", 443), (PUBLIC_IP, 443)]
    assert backend.record["tls"] == ("public.test", True, ssl.CERT_REQUIRED)
    assert backend.record["sent"].startswith(b"GET /path?q=1 HTTP/1.1\r\nHost: public.test\r\n")


async def test_a_stalled_lookup_ends_with_the_connect_timeout(monkeypatch):
    async def stalled(host: str, port: int | None = None) -> list[str]:
        await asyncio.sleep(3600)
        return [PUBLIC_IP]

    monkeypatch.setattr(netguard, "resolve", stalled)
    async with httpx.AsyncClient(transport=netguard.PublicOnlyTransport(network_backend=FakeBackend())) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("https://slow-dns.test/", timeout=0.2)


# --------------------------------------------------------------------------------------
# Alert webhooks: bounded, and the secret URL never reaches the logs
# --------------------------------------------------------------------------------------


def with_webhook(company: Company, url: str) -> Company:
    return company.model_copy(update={"notify": company.notify.model_copy(update={"slack_webhook_url": url})})


@pytest.fixture
def hot_lead(company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Ann", title="Travel Manager"))
    return lead.model_copy(update={"score": 90})


@pytest.fixture
def public_hosts(monkeypatch):
    async def public(_url: str) -> None:
        return None

    monkeypatch.setattr(notify, "assert_public_host", public)


SECRET_HOOK = "https://hooks.slack.com/services/T0001/B0001/SuperSecretToken123"


@pytest.mark.parametrize("status", [404, 429, 500, 302])
async def test_failed_webhook_logs_host_and_status_but_not_the_url(company, hot_lead, public_hosts, caplog, status):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, headers={"location": "http://10.0.0.1/"}))
    with caplog.at_level(logging.WARNING, logger="openberry.notify"):
        async with httpx.AsyncClient(transport=transport) as client:
            assert await notify.notify_hot_leads(with_webhook(company, SECRET_HOOK), [hot_lead], client=client) == []
    assert f"slack webhook (hooks.slack.com) failed: HTTP {status}" in caplog.text
    assert "SuperSecretToken123" not in caplog.text and "/services/" not in caplog.text


async def test_cli_logging_keeps_successful_webhook_urls_out_of_the_log(company, hot_lead, public_hosts, caplog):
    """httpx logs every request's full URL at INFO, and the CLI logs at INFO: that would print the webhook secret."""
    from openberry import cli

    httpx_log = logging.getLogger("httpx")
    level = httpx_log.level
    try:
        with pytest.raises(SystemExit):
            cli.main(["scan", "--source", "bogus"])  # configures logging like every command, then stops
        transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with caplog.at_level(logging.INFO):
            async with httpx.AsyncClient(transport=transport) as client:
                assert await notify.notify_hot_leads(with_webhook(company, SECRET_HOOK), [hot_lead],
                                                     client=client) == ["slack"]
        assert "SuperSecretToken123" not in caplog.text
    finally:
        httpx_log.setLevel(level)


async def test_webhook_response_body_is_never_downloaded(company, hot_lead, public_hosts):
    produced = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal produced
        chunk = b"x" * 65536
        for _ in range(320):  # 20 MB
            produced += len(chunk)
            yield chunk

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=endless()))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await notify.notify_hot_leads(with_webhook(company, SECRET_HOOK), [hot_lead], client=client) == ["slack"]
    assert produced <= 65536


async def test_slow_webhook_is_abandoned_after_the_total_timeout(company, hot_lead, public_hosts, monkeypatch, caplog):
    monkeypatch.setattr(notify, "WEBHOOK_TIMEOUT", 0.2)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        sent = await asyncio.wait_for(
            notify.notify_hot_leads(with_webhook(company, SECRET_HOOK), [hot_lead], client=client), 5)
    assert sent == []
    assert "slack webhook (hooks.slack.com) failed: TimeoutError" in caplog.text

