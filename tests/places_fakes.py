"""Offline fakes for the Google Maps businesses tests: Google's Text Search (New) and the businesses' own websites.

Fixtures in tests/fixtures/google_places: Text Search (New) pages as Google answers them for the field mask
places.id,places.websiteUri,nextPageToken, plus fields we never ask for (displayName, formattedAddress,
nationalPhoneNumber) carrying *-MARKER strings, so a test can prove nothing but the Place ID is kept; AIP-193 error
bodies; and business websites (WordPress homepage + contact page, obfuscated emails, schema.org JSON-LD, a chain
hotel's page, Cloudflare email protection, junk addresses) with their robots.txt files.
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from openberry import sitecontacts
from openberry.collectors import google_places
from openberry.netguard import UnsafeURL

FIXTURES = Path(__file__).parent / "fixtures" / "google_places"
QUERY = "event management companies in Dubai"
TOKEN2 = "AeCrKXsPAGETWO-token_value"
TOKEN3 = "AeCrKXsPAGETHREE-token_value"
GOOGLE_KEY = "AIzaSyD-test_key_0123456789abcdefghi4f2c"
MARKERS = ("GOOGLE-NAME-MARKER", "GOOGLE-ADDRESS-MARKER", "GOOGLE-PHONE-MARKER", "GOOGLE-UTM-MARKER")
PRIVATE_HOSTS = {"intranet.local", "10.0.0.5", "localhost", "169.254.169.254"}

ACME, DESERT = "ChIJAcmeEvents00001", "ChIJDesertDmc000002"
NO_SITE, FACEBOOK = "ChIJNoWebsite000003", "ChIJFacebookOnly004"
GULF, PALMCREST, DOWN = "ChIJGulfLaw00000005", "ChIJPalmcrest000006", "ChIJDownSite0000007"
PRIVATE_CLUB, CLOUDFLARE, JUNK = "ChIJPrivateClub0008", "ChIJCloudflare00009", "ChIJJunkFree0000010"

Override = httpx.Response | Callable[[httpx.Request], httpx.Response] | str


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def google_json(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


def google_error(name: str) -> httpx.Response:
    data = fixture_json(name)
    return httpx.Response(data["error"]["code"], json=data)


def html(body: str, status: int = 200, content_type: str = "text/html; charset=UTF-8") -> httpx.Response:
    text = fixture_text(body) if body.endswith(".html") else body
    return httpx.Response(status, text=text, headers={"content-type": content_type})


def plain(body: str, status: int = 200) -> httpx.Response:
    text = fixture_text(body) if body.endswith(".txt") else body
    return httpx.Response(status, text=text, headers={"content-type": "text/plain"})


def redirect(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


def search_page(places: list[dict[str, Any]], token: str = "") -> httpx.Response:
    return google_json({"places": places, **({"nextPageToken": token} if token else {})})


class FakeGoogle:
    """POST places:searchText, answered by (textQuery, pageToken). Unknown searches have no results."""

    def __init__(self, routes: dict[tuple[str, str], Override] | None = None) -> None:
        self.routes: dict[tuple[str, str], Override] = {
            (QUERY, ""): "search_page1.json", (QUERY, TOKEN2): "search_page2.json", (QUERY, TOKEN3): "search_last.json",
            **(routes or {}),
        }
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "POST" or str(request.url) != google_places.SEARCH_URL:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not found", "status": "NOT_FOUND"}})
        body = json.loads(request.content)
        target = self.routes.get((body.get("textQuery", ""), body.get("pageToken", "")))
        if target is None:
            return google_json({})
        if isinstance(target, str):
            return google_json(fixture_json(target))
        return target(request) if callable(target) else target

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


SITE_PAGES: dict[str, Override] = {
    "https://www.acme-events.ae/": "site_acme_home.html",
    "https://www.acme-events.ae/robots.txt": "robots_wordpress.txt",
    "https://acme-events.ae/robots.txt": "robots_wordpress.txt",
    "https://acme-events.ae/contact-us/": "site_acme_contact.html",
    "https://desertdmc.com/": "site_obfuscated.html",
    "https://gulflaw.ae/en/": "site_jsonld.html",
    "https://gulflaw.ae/robots.txt": "robots_disallow_contact.txt",
    "https://www.palmcrest-hotels.com/en-us/hotels/dxbpm-palmcrest-marina-hotel-dubai/overview/": "site_chain_hotel.html",
    "https://down.example-dead.ae/": lambda r: html("<h1>Service unavailable</h1>", 503),
    "https://private-club.ae/robots.txt": lambda r: plain("User-agent: *\nDisallow: /\n"),
    "https://cf-protected.ae/": "site_cloudflare.html",
    "http://junkfree.ae/": lambda r: redirect("https://junkfree.ae/"),
    "https://junkfree.ae/": "site_junk_emails.html",
}


class FakeSites:
    """Business websites by URL (query string ignored when there is no exact match); anything else is a 404."""

    def __init__(self, pages: dict[str, Override | None] | None = None) -> None:
        self.pages = {**SITE_PAGES, **(pages or {})}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        key = url if url in self.pages else url.split("?", 1)[0]
        target = self.pages.get(key)
        if target is None:
            return html("<h1>Not found</h1>", 404)
        if isinstance(target, str):
            return plain(target) if target.endswith(".txt") else html(target)
        return target(request) if callable(target) else target

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def hits(self, host: str) -> list[str]:
        return [u for u in self.urls if urlsplit(u).hostname == host]


async def fake_assert_public_host(url: str) -> None:
    host = urlsplit(url).hostname or ""
    if host in PRIVATE_HOSTS:
        raise UnsafeURL(f"{host} resolves to a non-public address")


def no_dns(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("a test tried a real DNS lookup")


def go_offline(monkeypatch: pytest.MonkeyPatch, sites: FakeSites | None = None) -> None:
    """No real DNS; public-host checks by name; business websites from `sites` through a mock client."""
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(sitecontacts, "assert_public_host", fake_assert_public_host)
    monkeypatch.setattr(google_places.GooglePlacesCollector, "page_interval", 0)
    if sites is not None:
        @asynccontextmanager
        async def site_client(ctx: Any) -> AsyncIterator[httpx.AsyncClient]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(sites),
                                         headers={"User-Agent": "OpenBerry-test"}) as client:
                yield client

        monkeypatch.setattr(google_places, "_site_client", site_client)
