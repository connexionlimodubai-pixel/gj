import asyncio

import httpx
import pytest

from openberry import website


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8000", "http://10.0.0.5", "http://[::1]/",
                                 "http://169.254.169.254/latest/meta-data"])
async def test_private_hosts_are_refused(url):
    with pytest.raises(website.UnsafeURL):
        await website.fetch_site_summary(url)


def test_non_http_schemes_are_refused():
    with pytest.raises(website.UnsafeURL):
        website.normalize_url("file:///etc/passwd")
    assert website.normalize_url("example.com") == "https://example.com"


async def test_fetch_follows_redirects_and_parses(monkeypatch):
    async def public(_url):
        return None

    monkeypatch.setattr(website, "assert_public_host", public)
    pages = {
        "https://acme.example/": httpx.Response(301, headers={"location": "https://www.acme.example/home"}),
        "https://www.acme.example/home": httpx.Response(200, headers={"content-type": "text/html"}, text=(
            "<html><head><title>Acme | Chauffeurs</title><meta property='og:description' content='VIP cars'>"
            "<script>var x = '<h1>not me</h1>';</script></head><body><h1>Arrive calm</h1><h2>Airport transfers</h2>"
            "<p>We drive executives across Dubai and Abu Dhabi, day and night, every day.</p></body></html>")),
    }
    transport = httpx.MockTransport(lambda req: pages[str(req.url)])
    async with httpx.AsyncClient(transport=transport) as client:
        summary = await website.fetch_site_summary("https://acme.example/", client=client)
    assert summary["site_name"] == "Acme"
    assert summary["description"] == "VIP cars"
    assert summary["headings"] == ["Arrive calm", "Airport transfers"]
    profile = website.suggest_profile(summary)
    assert profile["value_proposition"] == "Arrive calm" and profile["products"] == "Airport transfers"


async def test_redirect_to_private_host_is_refused(monkeypatch):
    calls = []

    async def guard(url):
        calls.append(url)
        if "internal" in url:
            raise website.UnsafeURL("private")

    monkeypatch.setattr(website, "assert_public_host", guard)
    transport = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "http://internal.local/"}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(website.UnsafeURL):
            await website.fetch_site_summary("https://acme.example", client=client)
    assert calls[-1].startswith("http://internal.local")


async def _public(_url):
    return None


def _html_client(content, content_type: str = "text/html") -> httpx.AsyncClient:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": content_type},
                                                               content=content))
    return httpx.AsyncClient(transport=transport)


async def test_only_the_first_max_bytes_of_a_page_are_downloaded(monkeypatch):
    monkeypatch.setattr(website, "assert_public_host", _public)
    chunk = b"<p>" + b"x" * 65_000 + b"</p>"
    produced = 0

    async def huge_page():
        nonlocal produced
        yield b"<html><head><title>Big | Site</title></head><body><h1>Welcome</h1>"
        for _ in range(320):  # about 20 MB
            produced += len(chunk)
            yield chunk

    async with _html_client(huge_page()) as client:
        summary = await website.fetch_site_summary("https://big.example/", client=client)
    assert summary["title"] == "Big | Site" and summary["headings"] == ["Welcome"]
    assert produced <= website.MAX_BYTES + len(chunk)


async def test_a_page_that_never_finishes_hits_the_total_time_limit(monkeypatch):
    monkeypatch.setattr(website, "assert_public_host", _public)
    monkeypatch.setattr(website, "FETCH_TIMEOUT", 0.3)

    async def drip():  # every read is quick, so per-read timeouts never fire
        while True:
            yield b"<p>still loading</p>"
            await asyncio.sleep(0.02)

    async with _html_client(drip()) as client:
        with pytest.raises(httpx.TimeoutException):
            await asyncio.wait_for(website.fetch_site_summary("https://slow.example/", client=client), 5)


async def test_page_charset_is_honoured(monkeypatch):
    monkeypatch.setattr(website, "assert_public_host", _public)
    page = "<html><head><title>Café Noël</title></head></html>".encode("latin-1")
    async with _html_client(page, "text/html; charset=iso-8859-1") as client:
        summary = await website.fetch_site_summary("https://cafe.example/", client=client)
    assert summary["title"] == "Café Noël"
