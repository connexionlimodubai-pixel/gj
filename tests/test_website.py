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
