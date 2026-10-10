"""Read a company's public website to pre-fill the registration form (name, description, offer).

Only public http(s) hosts are fetched (see netguard): private, loopback and link-local addresses
are refused so the dashboard can't be used to probe the network it runs in. Downloads are capped
in size and total time.
"""

from __future__ import annotations

import asyncio
import re
import zlib
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .config import get_settings
from .netguard import UnsafeURL, assert_public_host, public_client

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
FETCH_TIMEOUT = 30.0  # seconds for the whole fetch, redirects included


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise UnsafeURL("empty URL")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURL("only http(s) URLs are allowed")
    return url


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.headings: list[str] = []
        self.paragraphs: list[str] = []
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        if tag in ("title", "h1", "h2", "h3", "p", "li"):
            self._stack.append(tag)
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if not text:
                return
            if tag == "title" and not self.title:
                self.title = text
            elif tag in ("h1", "h2", "h3") and len(self.headings) < 15:
                self.headings.append(text)
            elif tag in ("p", "li") and len(text) > 40 and len(self.paragraphs) < 25:
                self.paragraphs.append(text)

    def handle_data(self, data: str) -> None:
        if self._stack and not self._skip:
            self._buf.append(data)


def parse_page(html: str, url: str = "") -> dict[str, Any]:
    parser = _PageParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML: keep whatever we parsed
        pass
    m = parser.meta
    site_name = m.get("og:site_name") or re.split(r"\s+[|\-–—:·]\s+", parser.title or "")[0]
    description = m.get("description") or m.get("og:description") or m.get("twitter:description") or ""
    text = " ".join(parser.paragraphs)
    return {
        "url": url,
        "site_name": site_name.strip(),
        "title": parser.title,
        "description": description,
        "headings": parser.headings,
        "text": text[:4000],
    }


async def fetch_site_summary(url: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Fetch a homepage and return {url, site_name, title, description, headings, text}."""
    url = normalize_url(url)
    own = client is None
    client = client or public_client(timeout=15, headers={"User-Agent": get_settings().user_agent})
    try:
        async with asyncio.timeout(FETCH_TIMEOUT):  # httpx timeouts are per read: a slow drip never ends
            return await _fetch_page(client, url)
    except TimeoutError as exc:
        raise httpx.ReadTimeout(f"no complete answer within {FETCH_TIMEOUT:g} seconds") from exc
    finally:
        if own:
            await client.aclose()


async def _fetch_page(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    for _ in range(MAX_REDIRECTS + 1):
        await assert_public_host(url)  # readable error; public_client() checks the connection itself too
        async with client.stream("GET", url, follow_redirects=False) as resp:
            if resp.is_redirect and resp.headers.get("location"):
                url = normalize_url(urljoin(url, resp.headers["location"]))
                continue
            resp.raise_for_status()
            if "html" not in resp.headers.get("content-type", "html"):
                raise UnsafeURL("URL did not return an HTML page")
            body = await read_capped(resp, MAX_BYTES)
            return parse_page(body.decode(resp.encoding or "utf-8", errors="replace"), str(resp.url))
    raise UnsafeURL("too many redirects")


async def read_capped(resp: httpx.Response, limit: int) -> bytes:
    """The first `limit` bytes of the body, decompressed; the rest is never downloaded.

    The body is decompressed here, at most `limit` bytes of it: httpx's aiter_bytes() decompresses each received
    chunk in full, so 300 KB of gzip could become 300 MB in memory. Raises httpx.DecodingError for an encoding we
    didn't ask for (we send Accept-Encoding: gzip, deflate) or a broken body.
    """
    if resp.is_stream_consumed:  # a body that was already in memory (httpx.Response(content=...)), decoded
        return resp.content[:limit]
    encoding = resp.headers.get("content-encoding", "").strip().lower()
    plain = encoding in ("", "identity")
    if not plain and encoding not in ("gzip", "x-gzip", "deflate"):  # "br", "gzip, gzip"...
        raise httpx.DecodingError(f"unsupported content encoding {encoding[:40]!r}", request=resp.request)
    decoder: Any = None
    head = b""
    chunks: list[bytes] = []
    size = 0
    async for raw in resp.aiter_raw():
        data = raw
        if not plain:
            if decoder is None:  # the first two bytes say which header the body has
                head += raw
                if len(head) < 2:
                    continue
                raw, head = head, b""
                zlib_header = raw[0] & 0x0F == 8 and int.from_bytes(raw[:2], "big") % 31 == 0
                # 47 = 32 + 15: a gzip or a zlib header; -15: "deflate" without its zlib header, as some servers send
                decoder = zlib.decompressobj(47 if encoding != "deflate" or zlib_header else -15)
            try:
                data = decoder.decompress(raw, limit - size)
            except zlib.error as exc:
                raise httpx.DecodingError(f"broken {encoding} body", request=resp.request) from exc
        chunks.append(data)
        size += len(data)
        if size >= limit:
            break
    return b"".join(chunks)[:limit]


_read_capped = read_capped  # the old name


def suggest_profile(summary: dict[str, Any]) -> dict[str, str]:
    """Turn a site summary into registration-form suggestions (the user reviews them)."""
    headings = [h for h in summary.get("headings", []) if 3 <= len(h) <= 120]
    value_prop = headings[0] if headings else ""
    desc = summary.get("description") or (summary.get("text") or "")[:300]
    return {
        "name": summary.get("site_name", "")[:200],
        "website": summary.get("url", ""),
        "description": desc[:600],
        "value_proposition": value_prop if value_prop.lower() != summary.get("site_name", "").lower() else "",
        "products": "; ".join(headings[1:6])[:600],
    }
