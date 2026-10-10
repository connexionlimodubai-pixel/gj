"""Reading a business's own website (sitecontacts.py), fully offline: robots.txt, fetching, parsing, emails, phones,
names and contact pages. Websites are an httpx.MockTransport (tests/places_fakes.py); real DNS lookups fail."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from openberry import sitecontacts
from openberry.sitecontacts import (
    RobotsRules,
    SiteReader,
    SiteSkipped,
    contact_links,
    deobfuscate,
    find_emails,
    is_junk_email,
    is_platform,
    is_shared_page,
    normalize_phone,
    parse_contact_page,
    rank_emails,
    site_name,
)
from places_fakes import FakeSites, fixture_text, go_offline, html, plain, redirect


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    go_offline(monkeypatch)


async def read(url: str, sites: FakeSites, *, budget: float = 60.0) -> sitecontacts.SiteContacts:
    async with httpx.AsyncClient(transport=httpx.MockTransport(sites)) as client:
        return await SiteReader(client, deadline=time.monotonic() + budget).read(url)


async def skipped(url: str, sites: FakeSites, **kwargs: float) -> SiteSkipped:
    with pytest.raises(SiteSkipped) as err:
        await read(url, sites, **kwargs)
    return err.value


# --------------------------------------------------------------------------------------
# robots.txt rules (RFC 9309)
# --------------------------------------------------------------------------------------


def allows(robots: str, path: str) -> bool:
    return RobotsRules.parse(robots).allows("https://site.example" + path)


def test_our_own_group_beats_the_star_group():
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: OpenBerry/2.0\nAllow: /\nDisallow: /private\n"
    assert allows(robots, "/contact") and not allows(robots, "/private/x")
    assert not RobotsRules.parse(fixture_text("robots_blocks_openberry.txt")).allows("https://site.example/")
    assert allows("User-agent: Googlebot\nDisallow: /\n", "/anything")  # no group for us or *: allowed


def test_groups_merge_and_user_agent_lines_share_rules():
    assert not allows("User-agent: openberry\nDisallow: /a\n\nUser-agent: OPENBERRY\nDisallow: /b\n", "/a")
    assert not allows("User-agent: openberry\nDisallow: /a\n\nUser-agent: OPENBERRY\nDisallow: /b\n", "/b")
    assert not allows("User-agent: foo\nUser-agent: *\nDisallow: /x\n", "/x/1")
    # A user-agent line after rules starts a new group.
    assert allows("User-agent: *\nDisallow: /x\nUser-agent: other\nDisallow: /y\n", "/y")


def test_the_longest_match_wins_and_allow_wins_a_tie():
    robots = "User-agent: *\nAllow: /\nDisallow: /contact\n"  # the stdlib parser would allow /contact here
    assert not allows(robots, "/contact-us/") and allows(robots, "/about/")
    assert allows("User-agent: *\nDisallow: /folder\nAllow: /folder/page\n", "/folder/page")
    assert not allows("User-agent: *\nDisallow: /folder\nAllow: /folder/page\n", "/folder/other")
    assert allows("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page")


def test_wildcards_end_anchors_comments_and_empty_rules():
    pdf = "User-agent: *\nDisallow: /*.pdf$\n"
    assert not allows(pdf, "/files/a.pdf") and allows(pdf, "/files/a.pdf?x=1") and allows(pdf, "/a.pdfx")
    assert not allows("User-agent: *\nDisallow: /*?\n", "/search?q=1") and allows("User-agent: *\nDisallow: /*?\n",
                                                                                    "/search")
    assert allows("User-agent: *\nDisallow:\n", "/anything")
    commented = "# site rules\nUser-agent: * # everyone\nDisallow: /private # keep out\n"
    assert not allows(commented, "/private") and allows(commented, "/public")
    assert allows("Disallow: /\nUser-agent: *\nAllow: /x\n", "/")  # rules before any user-agent are ignored
    assert allows("User-agent: *\r\nDisallow: /a\r\n", "/b") and not allows("User-agent: *\r\nDisallow: /a\r\n", "/a")


def test_percent_encoding_and_robots_txt_itself():
    assert not allows("User-agent: *\nDisallow: /%7Ejoe\n", "/~joe/page")
    assert not allows("User-agent: *\nDisallow: /ar/اتصل\n", "/ar/%D8%A7%D8%AA%D8%B5%D9%84")
    assert allows("User-agent: *\nDisallow: /\n", "/robots.txt")
    assert RobotsRules.disallow_all().allows("https://x.example/robots.txt")
    assert not RobotsRules.disallow_all().allows("https://x.example/")
    assert RobotsRules.allow_all().allows("https://x.example/anything")


def test_wordpress_default_robots_allows_the_contact_page():
    robots = fixture_text("robots_wordpress.txt")
    assert allows(robots, "/") and allows(robots, "/contact/") and allows(robots, "/wp-admin/admin-ajax.php")
    assert not allows(robots, "/wp-admin/options.php")


# --------------------------------------------------------------------------------------
# Reading a whole site
# --------------------------------------------------------------------------------------


async def test_homepage_then_contact_page_with_canonical_url_and_no_query():
    sites = FakeSites()
    site = await read("https://www.acme-events.ae/?utm_source=GOOGLE-UTM-MARKER&utm_medium=organic", sites)

    assert site.name == "Acme Events"
    assert site.url == "https://www.acme-events.ae/"
    assert (site.host, site.domain, site.shared) == ("acme-events.ae", "acme-events.ae", False)
    assert site.emails == ["info@acme-events.ae", "events@acme-events.ae", "jobs@acme-events.ae"]
    assert site.phones == ["+97145550101", "+971505550102"]
    assert site.pages == ["https://www.acme-events.ae/", "https://acme-events.ae/contact-us/"]
    assert site.description.startswith("Acme Events plans corporate events")
    # robots.txt for each host first; the about page, the PDF and the other site are never fetched.
    assert sites.urls == ["https://www.acme-events.ae/robots.txt",
                          "https://www.acme-events.ae/?utm_source=GOOGLE-UTM-MARKER&utm_medium=organic",
                          "https://acme-events.ae/robots.txt", "https://acme-events.ae/contact-us/"]
    assert all(r.headers["accept"].startswith(("text/html", "text/plain")) for r in sites.requests)


async def test_obfuscated_emails_on_the_own_domain_and_any_mailto():
    site = await read("https://desertdmc.com/", FakeSites())
    assert site.name == "Desert DMC"  # og:site_name; the title "Welcome" is generic
    assert site.emails == ["info@desertdmc.com", "sales@desertdmc.com", "bookings@desertdmc.com",
                           "desertdmc.dubai@gmail.com"]
    assert site.phones == ["+97145550123"]
    assert len(site.pages) == 1  # an own email and a phone on the homepage: no extra pages


async def test_json_ld_name_email_and_phone():
    site = await read("https://gulflaw.ae/en/", FakeSites())
    assert (site.name, site.emails, site.phones) == ("Gulf Law Partners", ["office@gulflaw.ae"], ["+97145550199"])
    assert site.url == "https://gulflaw.ae/en/" and site.domain == "gulflaw.ae" and not site.shared


async def test_a_contact_page_robots_disallows_is_not_fetched():
    sites = FakeSites({
        "https://gulflaw.ae/en/": lambda r: html(
            '<title>Gulf Law Partners</title><a href="/en/contact/">Contact</a><a href="/en/about/">About us</a>'
            '<a href="tel:+97145550199">Call</a>'),
        "https://gulflaw.ae/en/about/": lambda r: html('<p><a href="mailto:office@gulflaw.ae">Email us</a></p>'),
    })
    site = await read("https://gulflaw.ae/en/", sites)
    assert site.emails == ["office@gulflaw.ae"] and site.phones == ["+97145550199"]
    assert "https://gulflaw.ae/en/contact/" not in sites.urls
    assert site.pages == ["https://gulflaw.ae/en/", "https://gulflaw.ae/en/about/"]


async def test_a_chain_hotel_page_is_shared_and_gets_no_domain():
    sites = FakeSites()
    site = await read(
        "https://www.palmcrest-hotels.com/en-us/hotels/dxbpm-palmcrest-marina-hotel-dubai/overview/?scid=x", sites)
    assert site.shared and site.domain == "" and site.host == "palmcrest-hotels.com"
    assert site.name == "Palmcrest Marina Hotel Dubai"
    assert site.phones == ["+97145550140"]
    assert site.url == "https://www.palmcrest-hotels.com/en-us/hotels/dxbpm-palmcrest-marina-hotel-dubai/overview/"
    assert len(sites.requests) == 2  # robots.txt and the page: no extra pages on a shared site


async def test_cloudflare_protected_emails_are_not_decoded():
    site = await read("https://cf-protected.ae/", FakeSites())
    assert site.emails == [] and site.phones == ["+97145550188"]
    assert site.name == "CF Protected Travel"


async def test_junk_is_dropped_role_addresses_come_first_and_at_most_five():
    sites = FakeSites()
    site = await read("http://junkfree.ae", sites)
    assert site.emails == ["info@junkfree.ae", "john.smith@junkfree.ae", "a.one@junkfree.ae", "b.two@junkfree.ae",
                           "c.three@junkfree.ae"]
    assert site.phones == ["+97145550177"]  # "+971 (0)4 ..." without the trunk 0
    assert site.url == "https://junkfree.ae/" and site.name == "Junk Free Trading"
    assert sites.urls[:3] == ["http://junkfree.ae/robots.txt", "http://junkfree.ae/", "https://junkfree.ae/robots.txt"]


# --------------------------------------------------------------------------------------
# robots.txt answers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_robots_401_or_403_keeps_us_out(status: int):
    sites = FakeSites({"https://shop.example-biz.ae/robots.txt": lambda r: plain("no", status),
                       "https://shop.example-biz.ae/": lambda r: html("<title>Shop</title>")})
    err = await skipped("https://shop.example-biz.ae/", sites)
    assert err.reason == "robots"
    assert sites.urls == ["https://shop.example-biz.ae/robots.txt"]  # the homepage is never requested


@pytest.mark.parametrize("answer", [
    lambda r: plain("busy", 500), lambda r: plain("slow down", 429), lambda r: plain("bad gateway", 503)])
async def test_unreachable_robots_txt_skips_the_site(answer):
    sites = FakeSites({"https://shop.example-biz.ae/robots.txt": answer,
                       "https://shop.example-biz.ae/": lambda r: html("<title>Shop</title>")})
    err = await skipped("https://shop.example-biz.ae/", sites)
    assert (err.reason, err.detail) == ("robots", "robots.txt unreachable")
    assert len(sites.requests) == 1


async def test_robots_timeout_skips_the_site():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    sites = FakeSites({"https://shop.example-biz.ae/robots.txt": timeout})
    assert (await skipped("https://shop.example-biz.ae/", sites)).reason == "robots"


async def test_robots_404_allows_everything_and_redirects_are_followed():
    sites = FakeSites({
        "https://shop.example-biz.ae/": lambda r: html(
            '<title>Shop</title><a href="mailto:hi@shop.example-biz.ae">x</a>'),
        "http://old.example-biz.ae/robots.txt": lambda r: redirect("https://new.example-biz.ae/robots.txt"),
        "https://new.example-biz.ae/robots.txt": lambda r: plain("User-agent: *\nDisallow: /\n"),
        "http://old.example-biz.ae/": lambda r: html("<title>Old</title>"),
    })
    assert (await read("https://shop.example-biz.ae/", sites)).emails == ["hi@shop.example-biz.ae"]
    # robots.txt redirected to another host still applies, so the old site is not read.
    assert (await skipped("http://old.example-biz.ae/", sites)).reason == "robots"
    assert "http://old.example-biz.ae/" not in sites.urls


async def test_too_many_robots_redirects_count_as_unreachable():
    pages = {f"https://loop.example-biz.ae/r{n}/robots.txt": (lambda n: lambda r: redirect(
        f"https://loop.example-biz.ae/r{n + 1}/robots.txt"))(n) for n in range(10)}
    pages["https://loop.example-biz.ae/robots.txt"] = lambda r: redirect("https://loop.example-biz.ae/r0/robots.txt")
    sites = FakeSites(pages)
    assert (await skipped("https://loop.example-biz.ae/", sites)).reason == "robots"
    assert len(sites.requests) == sitecontacts.ROBOTS_REDIRECTS + 1


async def test_only_the_first_512_kb_of_robots_txt_are_read():
    body = "User-agent: *\n" + "Allow: /some/long/path/that/pads/the/file\n" * 15_000 + "Disallow: /\n"
    assert len(body) > 600_000
    sites = FakeSites({"https://big.example-biz.ae/robots.txt": lambda r: plain(body),
                       "https://big.example-biz.ae/": lambda r: html("<title>Big Robots Co</title>")})
    assert (await read("https://big.example-biz.ae/", sites)).name == "Big Robots Co"


async def test_robots_txt_is_fetched_once_per_host_per_scan():
    sites = FakeSites({
        "https://one.example-biz.ae/a/": lambda r: html("<title>A Co</title>"),
        "https://one.example-biz.ae/b/": lambda r: html("<title>B Co</title>"),
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(sites)) as client:
        reader = SiteReader(client, deadline=time.monotonic() + 60)
        names = await asyncio.gather(reader.read("https://one.example-biz.ae/a/"),
                                     reader.read("https://one.example-biz.ae/b/"))
        await reader.read("https://one.example-biz.ae/a/")
    assert [s.name for s in names] == ["A Co", "B Co"]
    assert sites.urls.count("https://one.example-biz.ae/robots.txt") == 1


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


async def test_social_pages_and_bad_addresses():
    sites = FakeSites({"https://moved.example-biz.ae/": lambda r: redirect("https://www.instagram.com/moved")})
    assert (await skipped("https://www.facebook.com/eventsbysara", sites)).reason == "no_website"
    assert (await skipped("https://m.facebook.com/x", sites)).reason == "no_website"
    assert (await skipped("https://moved.example-biz.ae/", sites)).reason == "no_website"
    assert (await skipped("ftp://files.example-biz.ae/", sites)).reason == "unreachable"
    assert (await skipped("", sites)).reason == "unreachable"
    assert "https://www.facebook.com/eventsbysara" not in sites.urls


async def test_redirect_to_another_domain_makes_it_the_site():
    sites = FakeSites({
        "https://old-brand.example-biz.ae/": lambda r: redirect("https://newbrand.ae/en"),
        "https://newbrand.ae/en": lambda r: html('<title>New Brand</title><a href="tel:+97145550000">call</a>'),
    })
    site = await read("https://old-brand.example-biz.ae/", sites)
    assert (site.host, site.domain, site.url) == ("newbrand.ae", "newbrand.ae", "https://newbrand.ae/en")


async def test_redirect_to_a_private_address_is_refused():
    sites = FakeSites({"https://shady.example-biz.ae/": lambda r: redirect("http://10.0.0.5/admin")})
    err = await skipped("https://shady.example-biz.ae/", sites)
    assert err.reason == "unreachable"
    assert not any("10.0.0.5" in u for u in sites.urls)


@pytest.mark.parametrize(("answer", "detail"), [
    (lambda r: httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}),
     "not an HTML page"),
    (lambda r: html("<h1>Gone</h1>", 410), "HTTP 410"),
    (lambda r: redirect("https://loop.example-biz.ae/"), "too many redirects"),
])
async def test_unusable_homepages(answer, detail: str):
    sites = FakeSites({"https://loop.example-biz.ae/": answer})
    err = await skipped("https://loop.example-biz.ae/", sites)
    assert (err.reason, err.detail) == ("unreachable", detail)


async def test_only_the_first_part_of_a_big_page_is_read():
    padding = "<p>" + "x" * 1000 + "</p>"
    body = "<title>Huge Page Co</title>" + padding * 1700 + '<a href="mailto:late@huge.example-biz.ae">late</a>'
    assert len(body) > sitecontacts.PAGE_MAX_BYTES
    sites = FakeSites({"https://huge.example-biz.ae/": lambda r: html(body)})
    site = await read("https://huge.example-biz.ae/", sites)
    assert site.name == "Huge Page Co" and site.emails == []


async def test_a_site_that_never_finishes_is_given_up(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sitecontacts, "SITE_TIME_LIMIT", 0.3)

    async def stuck(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return html("<title>Too late</title>")

    sites = FakeSites({"https://slow.example-biz.ae/": stuck})
    started = time.monotonic()
    err = await skipped("https://slow.example-biz.ae/", sites)
    assert (err.reason, err.detail) == ("unreachable", "too slow")
    assert time.monotonic() - started < 3


async def test_no_time_left_means_no_request():
    sites = FakeSites()
    err = await skipped("https://desertdmc.com/", sites, budget=-1)
    assert err.reason == "unreachable" and sites.requests == []


async def test_pages_are_parsed_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        calls.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    await read("https://desertdmc.com/", FakeSites())
    assert calls == ["parse_contact_page"]


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def page(body: str, url: str = "https://acme.ae/") -> sitecontacts.PageFacts:
    return parse_contact_page(body, url)


def test_mailto_links_are_cleaned_and_split():
    facts = page('<a href="mailto:Info%40Acme.ae,Sales@ACME.ae?subject=Hi%20there&cc=x@y.com">Mail</a>'
                 '<a href="MAILTO:Owner.Name@Gmail.com">Owner</a><a href="mailto:">empty</a>')
    assert find_emails(facts, "acme.ae") == ["info@acme.ae", "sales@acme.ae", "owner.name@gmail.com"]


def test_text_emails_only_on_the_sites_own_domain():
    facts = page("<p>Write to hello@acme.ae, dubai@branch.acme.ae or partner@other.com.</p>",
                 "https://www.acme.ae/")
    assert find_emails(facts, "acme.ae") == ["hello@acme.ae", "dubai@branch.acme.ae"]
    assert find_emails(page("<p>events@hotel.ae</p>"), "dubai.hotel.ae") == ["events@hotel.ae"]


def test_deobfuscation_only_rewrites_bracketed_words():
    assert deobfuscate("info [at] acme [dot] ae") == "info@acme.ae"
    assert deobfuscate("info(at)acme.ae, sales {AT} acme.ae, x[at]y(dot)ae") == "info@acme.ae, sales@acme.ae, x@y.ae"
    assert deobfuscate("meet us at acme.ae") == "meet us at acme.ae"
    assert find_emails(page("<p>Meet us at acme.ae (at) the expo.</p>"), "acme.ae") == []


@pytest.mark.parametrize("email", [
    "noreply@acme.ae", "no-reply@acme.ae", "donotreply@acme.ae", "do-not-reply@acme.ae", "no_reply@acme.ae",
    "mailer-daemon@acme.ae", "postmaster@acme.ae", "bounce@acme.ae", "you@example.com", "name@domain.com",
    "user@yourdomain.com", "logo@2x.png", "icon@3x.webp", "font@1x.woff2", "x@sentry-next.wixpress.com",
    "0123456789abcdef0123456789abcdef@o12.ingest.sentry.io", "a@test.com", "me@site.example", "x@host.local",
])
def test_junk_emails(email: str):
    assert is_junk_email(email)


@pytest.mark.parametrize("email", ["info@acme.ae", "events@acme-events.ae", "owner@gmail.com", "sara@pngtree.ae"])
def test_real_emails_are_not_junk(email: str):
    assert not is_junk_email(email)


def test_ranking_puts_the_own_domain_and_role_addresses_first():
    emails = ["john.smith@acme.ae", "owner@gmail.com", "sales@acme.ae", "info@acme.ae", "zed@acme.ae"]
    assert rank_emails(emails, "acme.ae") == ["info@acme.ae", "sales@acme.ae", "john.smith@acme.ae", "zed@acme.ae",
                                              "owner@gmail.com"]


@pytest.mark.parametrize(("raw", "phone"), [
    ("+971 4 555 0101", "+97145550101"), ("+971%204%20555%200101", "+97145550101"),
    ("00971 50 555 0102", "+971505550102"),
    ("04-555-0101", "045550101"), ("+44 (0)20 7946 0018", "+442079460018"), ("//+97145550101", "+97145550101"),
    ("123", ""), ("+1234567890123456", ""), ("", ""),
])
def test_phone_numbers(raw: str, phone: str):
    assert normalize_phone(raw) == phone


def test_site_names():
    facts = page('<title>Acme Events | Dubai</title><meta property="og:site_name" content="Acme Events LLC">')
    assert site_name(facts, "acme.ae", shared=False) == "Acme Events LLC"
    assert site_name(page("<title>Acme Events | Dubai</title>"), "acme.ae", shared=False) == "Acme Events"
    assert site_name(page("<title>Home</title>"), "acme.ae", shared=False) == "acme.ae"
    assert site_name(page(f"<title>{'Very long title ' * 20}</title>"), "acme.ae", shared=False) == "acme.ae"
    assert site_name(page("<title>  </title>"), "acme.ae", shared=False) == "acme.ae"
    shared = page('<title>Hotel X | Chain</title><meta property="og:site_name" content="Chain">'
                  '<meta property="og:title" content="Hotel X Downtown | Chain">')
    assert site_name(shared, "chain.com", shared=True) == "Hotel X Downtown"


def test_contact_links():
    facts = page(
        '<a href="/about-us/">About</a>'
        '<a href="https://www.acme.ae/contact-us/#form">Contact</a>'
        '<a href="https://acme.ae/contact-us/">Contact again</a>'
        '<a href="https://other.ae/contact/">Partner contact</a>'
        '<a href="/files/contact-sheet.pdf">Contact sheet</a>'
        '<a href="mailto:info@acme.ae">Contact by email</a>'
        '<a href="/ar/page-7/">اتصل بنا</a>'
        '<a href="/services/">Services</a>'
        '<a href="/#contact">This page</a>', "https://acme.ae/")
    assert contact_links(facts, "acme.ae") == ["https://www.acme.ae/contact-us/", "https://acme.ae/contact-us/",
                                               "https://acme.ae/ar/page-7/", "https://acme.ae/about-us/"]


@pytest.mark.parametrize(("url", "shared"), [
    ("https://acme.ae/", False), ("https://acme.ae/en/home", False), ("https://acme.ae/contact-us/", False),
    ("https://acme.ae/en-us/index.html", False),
    ("https://www.palmcrest-hotels.com/en-us/hotels/dxbpm-palmcrest-marina-hotel-dubai/overview/", True),
    ("https://chain.com/hotels/dubai/", True),
])
def test_shared_pages(url: str, shared: bool):
    assert is_shared_page(url) is shared


def test_platforms():
    assert is_platform("www.facebook.com") and is_platform("m.facebook.com") and is_platform("wa.me")
    assert is_platform("sites.google.com") and is_platform("Booking.com")
    assert not is_platform("notfacebook.com") and not is_platform("acme.ae")


def test_broken_html_and_json_ld_never_raise():
    facts = page('<html><head><link rel="canonical" href="http://[bad"><script type="application/ld+json">{"name":'
                 '</script><title>Still Here</title></head><body><a href="http://[::1">x</a><p>info@acme.ae')
    assert facts.title == "Still Here" and facts.jsonld == []
    assert find_emails(facts, "acme.ae") == ["info@acme.ae"]
