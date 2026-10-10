"""Read a business's own website for the contact details it publishes: name, emails, phone numbers, description.

Used by the Google Maps businesses source (collectors/google_places.py). Google's terms let OpenBerry keep only each
business's Place ID, so everything else about a business comes from what its own website publishes.

What is read
    The homepage, plus at most MAX_EXTRA_PAGES same-site pages that look like contact or about pages (by their
    address or link text, English and Arabic). The extra pages are skipped when the homepage already lists an email
    on the site's own domain and a phone number. For a page inside a bigger site (a hotel's page on its chain's
    website: see is_deep_page) only pages under its own address are read (own_prefix), not the chain's.

What is kept
    name         schema.org JSON-LD name of the Organization/LocalBusiness/Hotel..., else og:site_name, else the
                 first usable part of the <title> ("Acme Events | Dubai" -> "Acme Events", "Home - Sandstone Events"
                 -> "Sandstone Events"), else the host name. Page words (Home, Contact us, About us...) are skipped.
                 A page inside a bigger site is named by its own og:title or title, plus the site's brand when the
                 name doesn't already carry it ("Dubai | Beta Legal" -> "Dubai – Beta Legal"), and gets no domain:
                 two firms' Dubai office pages, or a chain's hotels, must not merge into one lead.
    emails       mailto: links and JSON-LD `email` for any domain (a business may use Gmail), except junk (noreply,
                 example domains, image file names such as logo@2x.png, error trackers, site builders); addresses in
                 the visible text, including "info [at] acme [dot] ae" and "info(at)acme.ae", only on the site's own
                 domain. Addresses hidden by an email-protection service (Cloudflare's /cdn-cgi/l/email-protection)
                 are not decoded: the site chose to hide them from robots. Role addresses (info@, contact@...) first.
    phones       tel: links and JSON-LD `telephone` only: numbers in plain text give too many false hits.
    description  the meta description, at most 300 characters.
    url          the site's canonical URL on the same host, else the address it ended up at, without the query
                 string (Google's links often carry ?utm_source=...).

Limits and politeness
    robots.txt is honoured (RFC 9309) for the product token "openberry", else "*", with our own small matcher: the
    longest matching rule wins and Allow wins a tie (urllib.robotparser applies the first matching rule); wildcards
    are matched in linear time, never with a regular expression a hostile robots.txt could make backtrack. 401/403
    means "keep out"; 429, 5xx, a timeout or too many redirects mean the site is skipped; other 4xx mean no rules.
    robots.txt is fetched once per host per scan. Only public addresses are fetched (netguard), every redirect hop
    is checked, each page is capped at PAGE_MAX_BYTES (after decompression) and PAGE_TIMEOUT, one business at
    SITE_TIME_LIMIT, and pages and robots.txt files are parsed and searched in a worker thread (page_details), so the
    dashboard sharing the event loop stays responsive: asyncio.timeout can't interrupt work on the loop itself.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx

from . import website
from .netguard import UnsafeURL, assert_public_host
from .repo import normalize_domain

PAGE_MAX_BYTES = website.MAX_BYTES          # 1.5 MB, only the first part is downloaded
PAGE_TIMEOUT = 10.0                          # seconds per request (connect + first byte + body)
ROBOTS_MAX_BYTES = 512_000                   # RFC 9309 §2.5: parse at least 500 KiB
ROBOTS_TIMEOUT = 8.0
PAGE_REDIRECTS = website.MAX_REDIRECTS       # 4
ROBOTS_REDIRECTS = 5                         # RFC 9309 §2.3.1.2
SITE_TIME_LIMIT = 20.0                       # seconds for one business (robots + homepage + extra pages)
MAX_EXTRA_PAGES = 2
MAX_EMAILS = 5
MAX_PHONES = 3
MAX_LINKS = 500
MAX_TEXT_CHARS = 200_000                     # visible text scanned for emails, per page
MAX_JSONLD_BLOCKS, MAX_JSONLD_CHARS = 5, 100_000
DESCRIPTION_LIMIT = 300
ROBOTS_TOKEN = "openberry"                   # product token of Settings.user_agent ("OpenBerry/0.1 (...)")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
PAGE_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
ROBOTS_ACCEPT = "text/plain,*/*;q=0.1"

# Social networks, link pages and marketplaces: a business whose "website" is one of these has no website of its own.
PLATFORM_HOSTS = ("facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
                  "tiktok.com", "youtube.com", "youtu.be", "wa.me", "whatsapp.com", "t.me", "linktr.ee",
                  "linkin.bio", "google.com", "goo.gl", "g.page", "business.site", "booking.com",
                  "tripadvisor.com", "yelp.com", "foursquare.com", "zomato.com", "talabat.com", "dubizzle.com")
GENERIC_NAMES = {"home", "homepage", "home page", "welcome", "index", "untitled", "default", "main page",
                 "website", "official website", "coming soon", "under construction",
                 # page words: "Contact Us - Alpha Trading" is Alpha Trading's contact page, not a company
                 "contact", "contacts", "contact us", "get in touch", "about", "about us", "who we are", "overview",
                 "offices", "our offices", "locations", "our locations", "اتصل بنا", "تواصل معنا", "من نحن",
                 "الرئيسية"}
ROLE_ORDER = ("info", "contact", "enquiries", "enquiry", "inquiries", "inquiry", "hello", "sales",
              "reservations", "reservation", "booking", "bookings", "events", "office", "admin", "support")
CONTACT_HINTS = ("contact", "get-in-touch", "getintouch", "reach-us", "enquir", "inquir", "kontakt",
                 "contacto", "اتصل", "تواصل")
ABOUT_HINTS = ("about", "who-we-are", "our-company", "company-profile", "من نحن", "من-نحن")
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".doc", ".docx",
                   ".xls", ".xlsx", ".mp4", ".mp3")
BUSINESS_TYPES = ("organization", "localbusiness", "business", "hotel", "agency", "service", "store", "restaurant",
                  "corporation")

EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@"
                      r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,8}[A-Za-z]{2,24})(?![A-Za-z0-9-])")
# On text whose whitespace is collapsed to single spaces: the gaps are bounded, so matching stays linear.
AT_RE = re.compile(r"\s?[\[\(\{]\s?at\s?[\]\)\}]\s?", re.I)      # info [at] x.ae, info(at)x.ae, info {at} x.ae
DOT_RE = re.compile(r"\s?[\[\(\{]\s?dot\s?[\]\)\}]\s?", re.I)    # x [dot] ae
_JUNK_LOCAL_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "no_reply")
_JUNK_LOCALS = {"mailer-daemon", "postmaster", "bounce", "bounces",
                "youremail", "your.email", "yourname", "your.name", "email", "name", "user", "username"}
# Example and site-builder placeholder addresses: GoDaddy's filler@godaddy.com, Canva's hello@reallygreatsite.com.
_JUNK_DOMAINS = ("example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "your-domain.com",
                 "email.com", "test.com", "mysite.com", "company.com", "sentry.io", "wixpress.com", "wix.com",
                 "godaddy.com", "reallygreatsite.com", "yourwebsite.com", "yoursite.com", "website.com",
                 "yourcompany.com")
_JUNK_SITE_LABEL = re.compile(r"domain|example|sample|website|mysite|your-?(?:domain|site|website|web|company|"
                              r"companyname|business|brand|email|mail|name|url)")    # the name part of the domain
_SECOND_LEVELS = {"co", "com", "net", "org", "ac", "gov", "edu"}   # yourdomain.co.uk, domain.com.au
_JUNK_DOMAIN_PARTS = ("sentry", "wixpress")
_JUNK_TLDS = {"example", "invalid", "local", "localhost", "test"}
_FILE_SUFFIXES = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tif", "tiff", "css", "js", "json", "pdf",
                  "mp4", "webm", "woff", "woff2", "ttf"}
_HEX_LOCAL = re.compile(r"[0-9a-f]{24,}")
_LANGUAGE_SEGMENT = re.compile(r"[a-z]{2}(-[a-z]{2,4})?")
_HOME_SEGMENTS = {"home", "homepage", "home-page", "home.html", "home.htm", "home.php", "home.aspx", "index",
                  "index.html", "index.htm", "index.php", "index.asp", "index.aspx", "default.asp", "default.aspx",
                  "default.htm", "default.html"}
_TITLE_SEPARATOR = re.compile(r" [|\-–—:·] ")   # on whitespace-collapsed text
_WELCOME_TO = re.compile(r"welcome to ", re.I)
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass
class SiteContacts:
    url: str            # the site address to store: canonical URL on the same host, else the final URL, no query
    host: str           # final host, lowercase, no "www."
    domain: str         # normalize_domain(host); "" when `shared`
    shared: bool        # the page is inside a site, not its home page: the site may be bigger than this business
                        # (a hotel on its chain's site, a law firm's Dubai office page); see is_deep_page
    name: str
    description: str
    emails: list[str] = field(default_factory=list)   # best first, at most MAX_EMAILS
    phones: list[str] = field(default_factory=list)   # at most MAX_PHONES
    pages: list[str] = field(default_factory=list)    # URLs read


class SiteSkipped(Exception):
    """The business's site gave nothing to use. reason:
    "no_website"  its website is a social network or marketplace page;
    "robots"      robots.txt keeps us out, or can't be reached;
    "unreachable" no answer, an error status, not HTML, too slow, or not a public address."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class PageFacts:
    """One parsed page; pure data."""

    url: str
    title: str = ""
    meta: dict[str, str] = field(default_factory=dict)     # lowercased name/property -> content (first wins)
    canonical: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute URL without fragment, anchor text)
    mailtos: list[str] = field(default_factory=list)
    tels: list[str] = field(default_factory=list)
    text: str = ""                                          # visible text, at most MAX_TEXT_CHARS
    jsonld: list[Any] = field(default_factory=list)         # parsed blocks


@dataclass
class PageDetails:
    """A parsed page and the contact details on it."""

    facts: PageFacts
    emails: list[str]
    phones: list[str]
    links: list[str]    # contact_links: same-site contact pages, then about pages


# --------------------------------------------------------------------------------------
# robots.txt (RFC 9309)
# --------------------------------------------------------------------------------------


def _canon_path(value: str) -> str:
    """Percent-encode what must be encoded and decode unreserved characters, so both sides compare alike."""
    value = quote(value, safe="!#$&'()*+,/:;=?@[]~%-._")

    def fix(m: re.Match[str]) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()

    return _PERCENT.sub(fix, value)


@dataclass(frozen=True)
class _Rule:
    """One Allow/Disallow pattern: the parts between its '*' wildcards, and whether '$' ends it."""

    parts: tuple[str, ...]
    anchored: bool

    @classmethod
    def compile(cls, pattern: str) -> _Rule:
        anchored = pattern.endswith("$")
        return cls(tuple(_canon_path(pattern[:-1] if anchored else pattern).split("*")), anchored)

    def matches(self, path: str) -> bool:
        """Linear time, like Google's robots.txt matcher: each part is found with str.find after the previous one
        (the leftmost place is always the best one), never with a backtracking regular expression."""
        first, *rest = self.parts
        if not path.startswith(first):
            return False
        if not rest:
            return path == first if self.anchored else True
        pos = len(first)
        *middle, last = rest
        for part in middle:
            found = path.find(part, pos)
            if found < 0:
                return False
            pos = found + len(part)
        if self.anchored:
            return path.endswith(last) and len(path) - len(last) >= pos
        return path.find(last, pos) >= 0


class RobotsRules:
    """The rules of one robots.txt for our product token (or "*")."""

    def __init__(self, rules: list[tuple[bool, str]] | None = None, *, everything: bool | None = None) -> None:
        self.rules = [(allow, len(pattern), _Rule.compile(pattern)) for allow, pattern in rules or []]
        self.everything = everything  # True: allow all, False: disallow all, None: use the rules

    @classmethod
    def allow_all(cls) -> RobotsRules:
        return cls(everything=True)

    @classmethod
    def disallow_all(cls) -> RobotsRules:
        return cls(everything=False)

    @classmethod
    def parse(cls, text: str, token: str = ROBOTS_TOKEN) -> RobotsRules:
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        in_rules = False
        for raw in re.split(r"\r\n|\r|\n", text or ""):
            line = raw.split("#", 1)[0].strip()
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:  # a user-agent after rules starts a new group
                    groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(value.split("/")[0].strip().lower())
            elif key in ("allow", "disallow") and agents:  # rules before any user-agent are ignored
                in_rules = True
                if value:  # an empty value is no rule
                    rules.append((key == "allow", value))
        if agents:
            groups.append((agents, rules))
        token = token.lower()
        for wanted in (token, "*"):
            matched = [r for names, group in groups if wanted in names for r in group]
            if any(wanted in names for names, _ in groups):
                return cls(matched)
        return cls.allow_all()

    def allows(self, url: str) -> bool:
        if self.everything is not None:
            return self.everything or urlsplit(url).path == "/robots.txt"
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        target = _canon_path(path + (f"?{parts.query}" if parts.query else ""))
        best_len, allowed = -1, True
        for allow, length, rule in self.rules:
            if (length > best_len or (length == best_len and allow)) and rule.matches(target):
                best_len, allowed = length, allow
        return allowed


# --------------------------------------------------------------------------------------
# Page parsing (pure functions)
# --------------------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "td", "th", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "nav", "address", "form", "label", "dt", "dd", "hr",
               "blockquote", "option", "main", "aside"}


class _ContactParser(HTMLParser):
    def __init__(self, url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.facts = PageFacts(url=url)
        self._skip = 0
        self._in_title = False
        self._title: list[str] = []
        self._text: list[str] = []
        self._text_len = 0
        self._jsonld: list[str] | None = None
        self._anchor: tuple[str, list[str]] | None = None

    def _add_text(self, data: str) -> None:
        if self._text_len < MAX_TEXT_CHARS:
            self._text.append(data)
            self._text_len += len(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").strip().lower() == "application/ld+json":
            self._jsonld = []
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag in _BLOCK_TAGS:
            self._add_text(" ")
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").strip().lower()
            if key and a.get("content", "").strip():
                self.facts.meta.setdefault(key, a["content"].strip())
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split() and a.get("href"):
            try:
                self.facts.canonical = self.facts.canonical or urljoin(self.facts.url, a["href"].strip())
            except ValueError:  # an invalid address must not stop the parsing
                pass
        elif tag == "a" and a.get("href"):
            self._anchor = (a["href"].strip(), [])

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            if tag == "script" and self._jsonld is not None:
                self._add_jsonld("".join(self._jsonld))
                self._jsonld = None
            return
        if tag in _BLOCK_TAGS:
            self._add_text(" ")
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._anchor is not None:
            self._add_link(*self._anchor)
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._jsonld is not None:
            self._jsonld.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self._title.append(data)
            return
        self._add_text(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def _add_jsonld(self, raw: str) -> None:
        if len(self.facts.jsonld) >= MAX_JSONLD_BLOCKS or len(raw) > MAX_JSONLD_CHARS:
            return
        try:
            self.facts.jsonld.append(json.loads(raw))
        except ValueError:
            pass

    def _add_link(self, href: str, text_parts: list[str]) -> None:
        lower = href.lower()
        if lower.startswith("mailto:"):
            for part in unquote(href[7:]).split("?")[0].split(","):
                if part.strip():
                    self.facts.mailtos.append(part.strip())
        elif lower.startswith("tel:"):
            self.facts.tels.append(unquote(href[4:]).strip())
        elif len(self.facts.links) < MAX_LINKS:
            try:
                url = urljoin(self.facts.url, href).split("#", 1)[0]
                web = urlsplit(url).scheme in ("http", "https")
            except ValueError:  # e.g. an invalid IPv6 address in the link
                return
            if web:
                self.facts.links.append((url, " ".join(" ".join(text_parts).split())[:100]))

    def finish(self) -> PageFacts:
        self.facts.title = " ".join(" ".join(self._title).split())
        self.facts.text = "".join(self._text)[:MAX_TEXT_CHARS]
        return self.facts


def parse_contact_page(html: str, url: str) -> PageFacts:
    """Title, meta tags, canonical link, links, mailto:/tel: links, visible text and JSON-LD of a page."""
    parser = _ContactParser(url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML: keep whatever was parsed
        pass
    return parser.finish()


def page_details(html: str, url: str) -> PageDetails:
    """parse_contact_page, then the emails, phones and contact links on the page. Every regular expression that reads
    the page runs here, so SiteReader runs it all in a worker thread."""
    facts = parse_contact_page(html, url)
    host = bare_host(url)
    return PageDetails(facts, find_emails(facts, host), find_phones(facts), contact_links(facts, host))


def _jsonld_objects(data: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Every JSON object in a JSON-LD block (lists and @graph included), outermost first."""
    if depth > 6:
        return []
    if isinstance(data, list):
        return [o for item in data[:50] for o in _jsonld_objects(item, depth + 1)]
    if not isinstance(data, dict):
        return []
    out = [data]
    for value in data.values():
        if isinstance(value, (dict, list)):
            out.extend(_jsonld_objects(value, depth + 1))
    return out


def _jsonld_strings(page: PageFacts, key: str) -> list[str]:
    values: list[str] = []
    for obj in _jsonld_objects(page.jsonld):
        value = obj.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
    return values


def _is_business(obj: dict[str, Any]) -> bool:
    kinds = obj.get("@type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return any(isinstance(k, str) and any(t in k.lower() for t in BUSINESS_TYPES) for k in kinds)


def bare_host(url_or_host: str) -> str:
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    return (host or "").lower().rstrip(".").removeprefix("www.")


def same_site(email_or_host: str, site_host: str) -> bool:
    """The host (or an email's domain) is the site's host, or one is a subdomain of the other."""
    a = bare_host(email_or_host.rpartition("@")[2])
    b = bare_host(site_host)
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def deobfuscate(text: str) -> str:
    """'info [at] acme [dot] ae' -> 'info@acme.ae'. Only bracketed words: a bare ' at ' is never rewritten.
    Whitespace is collapsed first (a page builder's indentation can be a run of 100,000 spaces)."""
    text = " ".join((text or "").split())
    return DOT_RE.sub(".", AT_RE.sub("@", text))


def _site_label(domain: str) -> str:
    """'mail.yourdomain.co.uk' -> 'yourdomain': the name part of the domain."""
    labels = domain.split(".")[:-1]
    if len(labels) >= 2 and labels[-1] in _SECOND_LEVELS:
        labels.pop()
    return labels[-1] if labels else ""


def is_junk_email(email: str) -> bool:
    local, _, domain = email.lower().rpartition("@")
    if local.startswith(_JUNK_LOCAL_PREFIXES) or local in _JUNK_LOCALS or _HEX_LOCAL.fullmatch(local):
        return True
    if any(domain == d or domain.endswith("." + d) for d in _JUNK_DOMAINS):
        return True
    if _JUNK_SITE_LABEL.fullmatch(_site_label(domain)):  # name@domain.ae, email@yourdomain.ae, info@yourwebsite.com
        return True
    tld = domain.rpartition(".")[2]
    return any(part in domain for part in _JUNK_DOMAIN_PARTS) or tld in _JUNK_TLDS or tld in _FILE_SUFFIXES


def _clean_email(raw: str) -> str:
    email = raw.strip().removeprefix("mailto:").removeprefix("MAILTO:").strip().rstrip(".").lower()
    return email if EMAIL_RE.fullmatch(email) and not is_junk_email(email) else ""


def find_emails(page: PageFacts, site_host: str) -> list[str]:
    """Emails the page publishes, first seen first. Addresses in the text only on the site's own domain."""
    found: list[str] = []
    candidates = [(m, True) for m in page.mailtos] + [(e, True) for e in _jsonld_strings(page, "email")]
    candidates += [(m.group(1), False) for m in EMAIL_RE.finditer(deobfuscate(page.text))]
    for raw, any_domain in candidates:
        email = _clean_email(raw)
        if email and email not in found and (any_domain or same_site(email, site_host)):
            found.append(email)
    return found


def rank_emails(emails: list[str], site_host: str) -> list[str]:
    """On the site's own domain first, then role addresses (info@, contact@...), then as found."""
    def key(item: tuple[int, str]) -> tuple[int, int, int]:
        index, email = item
        local = email.partition("@")[0]
        role = ROLE_ORDER.index(local) if local in ROLE_ORDER else len(ROLE_ORDER)
        return (0 if same_site(email, site_host) else 1, role, index)

    return [email for _, email in sorted(enumerate(emails), key=key)]


def normalize_phone(raw: str) -> str:
    """'+971 4 555 0101' -> '+97145550101', '00971...' -> '+971...'; '' when it isn't 7-15 digits."""
    text = unquote(raw or "").strip().lstrip("/").replace("(0)", "")  # tel://..., +44 (0)20 ...
    plus = text.startswith(("+", "00"))
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    if not 7 <= len(digits) <= 15:
        return ""
    if len(set(digits)) == 1 or "123456789" in digits:  # a template's 123-456-7890 or 0000000
        return ""
    return "+" + digits if plus else digits


def find_phones(page: PageFacts) -> list[str]:
    phones: list[str] = []
    for raw in [*page.tels, *_jsonld_strings(page, "telephone")]:
        phone = normalize_phone(raw)
        if phone and phone not in phones:
            phones.append(phone)
    return phones


def _usable_name(name: str) -> bool:
    return 2 <= len(name) <= 100 and name.casefold() not in GENERIC_NAMES


def name_parts(title: str) -> list[str]:
    """The usable parts of a title: 'Home - Sandstone Events' -> ['Sandstone Events'], 'Welcome to X' -> ['X']."""
    parts = _TITLE_SEPARATOR.split(" ".join((title or "").split()))
    return [name for part in parts if _usable_name(name := _WELCOME_TO.sub("", part, count=1).strip())]


def site_name(page: PageFacts, host: str, shared: bool) -> str:
    """The business's name as its site gives it (see the module docstring), else the host."""
    business = [" ".join(str(obj.get("name") or "").split()) for obj in _jsonld_objects(page.jsonld)
                if _is_business(obj)][:1]
    if business and _usable_name(business[0]):
        return business[0]
    site = " ".join(page.meta.get("og:site_name", "").split())
    site = site if _usable_name(site) else ""
    titles = name_parts(page.title)
    if not shared:
        return site or (titles[0] if titles else host)
    # A page inside a bigger site: its own title ("Dubai | Beta Legal" -> "Dubai") plus the site's brand, so two
    # firms' "Dubai" office pages are two leads.
    parts = name_parts(page.meta.get("og:title", "")) + titles
    if not parts:
        return site or host
    name, brand = parts[0], site or (titles or parts)[-1]
    a, b = name.casefold(), brand.casefold()
    if b in a or a in b or set(re.findall(r"\w{4,}", b)) & set(re.findall(r"\w+", a)):
        return name  # "Palmcrest Marina Hotel Dubai" on the "Palmcrest Rewards" site
    return f"{name} – {brand}"[:100]


def _site_segments(url: str) -> list[str]:
    """The path's segments besides language codes and home/index: '/en-us/hotels/x/overview/' -> hotels, x, overview."""
    segments = [s for s in unquote(urlsplit(url).path).lower().split("/") if s]
    return [s for s in segments if not _LANGUAGE_SEGMENT.fullmatch(s) and s not in _HOME_SEGMENTS]


def is_deep_page(url: str) -> bool:
    """Not a site's home page: grandseasons.com/dubaijb/, a firm's /en/offices/dubai/. The site may hold other
    businesses (a chain's other hotels), so the page gets no domain and its own name: they must not merge."""
    return bool(_site_segments(url))


def is_shared_page(url: str) -> bool:
    """A page deep inside a bigger site, e.g. hotelchain.com/en-us/hotels/dxb-marina-hotel/overview/ (2+ path segments
    besides language codes and home/index)."""
    return len(_site_segments(url)) >= 2


def own_prefix(url: str) -> str:
    """The path under which a deep page's own pages live: '/en-us/hotels/dxb-x/overview/' -> '/en-us/hotels/dxb-x/'
    (its parent, while that is still deep inside: 2+ segments), '/dubaijb/' -> '/dubaijb/'."""
    path = unquote(urlsplit(url).path).lower()
    path = path if path.endswith("/") else path + "/"
    parent = path.rstrip("/").rpartition("/")[0] + "/"
    return parent if is_shared_page(parent) else path


def _path_under(url: str, prefix: str) -> bool:
    path = unquote(urlsplit(url).path).lower()
    return (path if path.endswith("/") else path + "/").startswith(prefix)


def is_platform(host: str) -> bool:
    host = bare_host(host)
    return any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


def _without_fragment_or_slash(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def contact_links(page: PageFacts, site_host: str) -> list[str]:
    """Same-site links that look like contact pages, then about pages; unique, in page order."""
    here = _without_fragment_or_slash(page.url)
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for index, (url, text) in enumerate(page.links):
        parts = urlsplit(url)
        path = unquote(parts.path).lower()
        if (bare_host(parts.hostname or "") != bare_host(site_host) or path.endswith(SKIP_EXTENSIONS)
                or _without_fragment_or_slash(url) == here or url in seen):
            continue
        hay = f"{path} {text.lower()}"
        if any(h in hay for h in CONTACT_HINTS):
            priority = 0
        elif any(h in hay for h in ABOUT_HINTS):
            priority = 1
        else:
            continue
        seen.add(url)
        ranked.append((priority, index, url))
    return [url for _, _, url in sorted(ranked)]


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", "", ""))


def _site_url(page: PageFacts, final_url: str) -> str:
    """The canonical URL when it is http(s) on the same host, else the final URL; no query or fragment."""
    final_host = bare_host(final_url)
    for candidate in (page.canonical, page.meta.get("og:url", "")):
        try:
            parts = urlsplit(candidate.strip())
            same = parts.scheme in ("http", "https") and bare_host(parts.hostname or "") == final_host
        except ValueError:  # e.g. an invalid IPv6 address
            continue
        if same:
            return _without_query(candidate.strip())
    return _without_query(final_url)


def _valid_host(host: str | None) -> bool:
    """Syntax only (as news._valid_hostname): 1-63 character labels, at most 253 characters."""
    host = (host or "").rstrip(".")
    if not host or len(host) > 253:
        return False
    return host.startswith("[") or ":" in host or all(0 < len(label) <= 63 for label in host.split("."))


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


class SiteReader:
    """Reads business websites during one scan: one client, robots.txt cached per scheme+host, a deadline
    (time.monotonic()) after which no site is read."""

    def __init__(self, client: httpx.AsyncClient, *, deadline: float) -> None:
        self.client = client
        self.deadline = deadline
        self._robots: dict[str, RobotsRules | None] = {}   # None: unreachable
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def read(self, url: str) -> SiteContacts:
        """The contact details a business's website publishes. Raises SiteSkipped, never anything else."""
        try:
            parts = urlsplit(website.normalize_url(url))
            url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        except (UnsafeURL, ValueError) as exc:
            raise SiteSkipped("unreachable", "not an http(s) address") from exc
        if is_platform(urlsplit(url).hostname or ""):
            raise SiteSkipped("no_website", bare_host(url))
        budget = min(SITE_TIME_LIMIT, self.deadline - time.monotonic())
        if budget <= 0:
            raise SiteSkipped("unreachable", "too slow")
        try:
            async with asyncio.timeout(budget):
                return await self._read(url)
        except SiteSkipped:
            raise
        except TimeoutError as exc:
            raise SiteSkipped("unreachable", "too slow") from exc
        except Exception as exc:  # one odd website never fails the scan
            raise SiteSkipped("unreachable", type(exc).__name__) from exc

    async def _read(self, url: str) -> SiteContacts:
        home, final = await self._fetch_html(url)
        page, host = home.facts, bare_host(final)
        # A home page that redirects to /en/welcome/ on the same host is still the business's own site.
        shared = is_deep_page(final) and not (bare_host(url) == host and not is_deep_page(url))
        emails, phones, links = list(home.emails), list(home.phones), home.links
        if shared:  # only the business's own pages: a hotel's contact page, not its chain's
            prefix = own_prefix(final)
            links = [link for link in links if _path_under(link, prefix)]
        pages = [_without_query(final)]
        for link in links[:MAX_EXTRA_PAGES]:
            if phones and any(same_site(e, host) for e in emails):
                break
            try:
                extra, extra_url = await self._fetch_html(link)
            except SiteSkipped:
                continue  # disallowed or failed: the homepage's details still count
            if bare_host(extra_url) != host:
                continue
            pages.append(_without_query(extra_url))
            emails += [e for e in extra.emails if e not in emails]
            phones += [p for p in extra.phones if p not in phones]
        description = " ".join((page.meta.get("description") or page.meta.get("og:description") or "").split())
        return SiteContacts(
            url=_site_url(page, final), host=host, domain="" if shared else normalize_domain(host), shared=shared,
            name=site_name(page, host, shared), description=description[:DESCRIPTION_LIMIT],
            emails=rank_emails(emails, host)[:MAX_EMAILS], phones=phones[:MAX_PHONES], pages=pages)

    async def _check_host(self, url: str, timeout: float) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not _valid_host(parts.hostname):
            raise SiteSkipped("unreachable", "not an http(s) address")
        try:
            await asyncio.wait_for(assert_public_host(url), timeout)
        except UnsafeURL as exc:
            raise SiteSkipped("unreachable", "not a public address") from exc
        except (TimeoutError, OSError, UnicodeError) as exc:
            raise SiteSkipped("unreachable", "host lookup failed") from exc

    async def _fetch_html(self, url: str) -> tuple[PageDetails, str]:
        """(parsed page, final URL), following redirects by hand: every hop is checked (public host, robots.txt)."""
        current = url
        for _ in range(PAGE_REDIRECTS + 1):
            if is_platform(urlsplit(current).hostname or ""):  # e.g. a site that moved to its Instagram page
                raise SiteSkipped("no_website", bare_host(current))
            await self._check_host(current, PAGE_TIMEOUT)
            rules = await self._robots_for(current)
            if rules is None:
                raise SiteSkipped("robots", "robots.txt unreachable")
            if not rules.allows(current):
                raise SiteSkipped("robots", "robots.txt disallows it")
            try:
                async with self.client.stream("GET", current, headers={"Accept": PAGE_ACCEPT}, follow_redirects=False,
                                              timeout=httpx.Timeout(PAGE_TIMEOUT)) as resp:
                    location = resp.headers.get("location", "").strip()
                    if resp.status_code in REDIRECT_STATUSES and location:
                        current = urljoin(str(resp.url), location)
                        continue
                    if not 200 <= resp.status_code < 300:
                        raise SiteSkipped("unreachable", f"HTTP {resp.status_code}")
                    content_type = resp.headers.get("content-type", "")
                    if content_type and "html" not in content_type.lower():
                        raise SiteSkipped("unreachable", "not an HTML page")
                    body = await website.read_capped(resp, PAGE_MAX_BYTES)
                    final = str(resp.url)
                    try:
                        html = body.decode(resp.encoding or "utf-8", errors="replace")
                    except LookupError:  # an unknown charset
                        html = body.decode("utf-8", errors="replace")
            except SiteSkipped:
                raise
            except UnsafeURL as exc:  # refused when connecting: the host now resolves to a non-public address
                raise SiteSkipped("unreachable", "not a public address") from exc
            except httpx.TimeoutException as exc:
                raise SiteSkipped("unreachable", "timed out") from exc
            except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
                raise SiteSkipped("unreachable", type(exc).__name__) from exc
            return await asyncio.to_thread(page_details, html, final), final
        raise SiteSkipped("unreachable", "too many redirects")

    async def _robots_for(self, url: str) -> RobotsRules | None:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc.lower()}"
        if key in self._robots:
            return self._robots[key]
        lock = self._robots_locks.setdefault(key, asyncio.Lock())
        async with lock:  # businesses read at the same time on one host fetch it once
            if key not in self._robots:
                self._robots[key] = await self._fetch_robots(key + "/robots.txt")
        return self._robots[key]

    async def _fetch_robots(self, url: str) -> RobotsRules | None:
        """The rules, or None when robots.txt can't be reached (then nothing on that host is read)."""
        current = url
        for _ in range(ROBOTS_REDIRECTS + 1):
            try:
                await self._check_host(current, ROBOTS_TIMEOUT)
            except SiteSkipped:
                return None
            try:
                async with self.client.stream("GET", current, headers={"Accept": ROBOTS_ACCEPT},
                                              follow_redirects=False, timeout=httpx.Timeout(ROBOTS_TIMEOUT)) as resp:
                    status = resp.status_code
                    location = resp.headers.get("location", "").strip()
                    if status in REDIRECT_STATUSES and location:
                        current = urljoin(str(resp.url), location)
                        continue
                    if 200 <= status < 300:
                        body = await website.read_capped(resp, ROBOTS_MAX_BYTES)
                        return await asyncio.to_thread(RobotsRules.parse, body.decode("utf-8", errors="replace"))
            except (httpx.HTTPError, httpx.InvalidURL, UnsafeURL, OSError, ValueError):
                return None
            if status in (401, 403):
                return RobotsRules.disallow_all()
            if 400 <= status < 500 and status != 429:
                return RobotsRules.allow_all()
            return None  # 429, 5xx: unreachable
        return None  # too many redirects
