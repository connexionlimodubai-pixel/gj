"""Dashboard parts of Google Maps businesses: the API keys page, the searches in the profile form, the Sources card,
the lead page and the source filter. Google is never called: check_key is replaced in every test that saves a key."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openberry import config, repo
from openberry.collectors import google_places
from openberry.models import CompanyIn, LeadIn, SignalIn
from openberry.web import forms, pages
from openberry.web.app import create_app
from places_fakes import ACME, GOOGLE_KEY, QUERY

KEY = "OPENBERRY_GOOGLE_PLACES_KEY"
LIMIT = "OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT"
CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)">')


@pytest.fixture
def web_settings(settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    settings.scheduler_enabled = False
    settings.http_mcp_enabled = False
    settings.allowed_hosts = ["testserver"]
    monkeypatch.setenv("OPENBERRY_ENV_FILE", str(tmp_path / "openberry.env"))
    monkeypatch.setattr(config, "_FROM_FILE", {})
    monkeypatch.setattr(config, "_settings_file_seen", None)
    for name in config.DASHBOARD_SETTINGS:  # unset now, and removed again after the test
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return settings


@pytest.fixture
def client(web_settings):
    with TestClient(create_app(web_settings)) as c:
        yield c


@pytest.fixture
def check_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """check_key never reaches Google: tests set the answer in calls[0]."""
    calls: list[Any] = [(True, "")]

    async def fake_check(key: str, client: Any = None) -> tuple[bool | None, str]:
        calls.append(key)
        return calls[0]

    monkeypatch.setattr(google_places, "check_key", fake_check)
    return calls


def token(client: TestClient, path: str = "/keys") -> str:
    match = CSRF_META.search(client.get(path).text)
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def post(client: TestClient, path: str, data: dict[str, Any], page: str = "/keys"):
    return client.post(path, data={**data, "csrf_token": token(client, page)}, follow_redirects=False)


def save_key(client: TestClient, key: str = GOOGLE_KEY):
    return post(client, "/keys/google-maps", {"action": "save_key", "key": key})


def env_file() -> Path:
    return Path(os.environ["OPENBERRY_ENV_FILE"])


def flashes(html: str) -> str:
    return " ".join(re.findall(r'class="flash[^"]*"[^>]*>.*?<p>(.*?)</p>', html, re.S))


# --------------------------------------------------------------------------------------
# API keys page
# --------------------------------------------------------------------------------------


def test_keys_page_in_local_mode(client):
    page = client.get("/keys")
    assert page.status_code == 200
    html = page.text
    assert "<h1>API keys</h1>" in html and 'href="/keys" aria-current="page"' in html
    assert "No key yet." in html and "Save key" in html and "Remove key" not in html
    assert 'type="password"' in html and 'name="key"' in html and 'autocomplete="off"' in html
    assert "0 of 900</strong> searches used this month. The count starts again on <span class=\"nowrap\">1 " in html
    assert str(env_file()) in html and "only your user account can read" in html
    assert "Restrict it to Places API (New)" in html and "respects each website's robots.txt" in html


def test_keys_page_needs_a_login(client, web_settings):
    web_settings.password = "pw"
    web_settings.public_registration = True  # visitors may register, but never see keys
    resp = client.get("/keys", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/keys"
    resp = client.post("/keys/google-maps", data={"action": "save_key", "key": GOOGLE_KEY}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    assert not env_file().exists()


def test_saving_needs_the_csrf_token(client):
    resp = client.post("/keys/google-maps", data={"action": "save_key", "key": GOOGLE_KEY})
    assert resp.status_code == 403 and not env_file().exists()


def test_save_check_and_never_show_the_key_again(client, company, check_calls):
    repo.update_company(company.id, {"signals": {"places_queries": [QUERY]}})
    resp = save_key(client)
    assert resp.status_code == 303 and resp.headers["location"] == "/keys#google-maps"
    assert check_calls[1:] == [GOOGLE_KEY]
    page = client.get("/keys").text
    assert "Google Maps key saved. Google accepted it." in flashes(page)
    assert "Key saved, ending in 4f2c." in page and "Replace key" in page and "Remove key" in page
    assert f'{KEY}="{GOOGLE_KEY}"' in env_file().read_text(encoding="utf-8")
    assert os.environ[KEY] == GOOGLE_KEY and client.app.state.settings.google_places_key == GOOGLE_KEY

    base = f"/c/{company.id}"
    for path in ("/keys", base, f"{base}/settings", f"{base}/leads", "/help", "/companies", "/register",
                 "/api/companies", f"/api/companies/{company.id}", f"/api/companies/{company.id}/stats"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert GOOGLE_KEY not in resp.text and GOOGLE_KEY[:-4] not in resp.text, path


@pytest.mark.parametrize(("answer", "flash"), [
    ((False, "Google says the API key is not valid. Check it on the API keys page."),
     "Google Maps key saved, but Google refused it: Google says the API key is not valid."),
    ((None, "ConnectError"), "Google Maps key saved. OpenBerry couldn&#39;t reach Google to check it (ConnectError)."),
])
def test_save_when_google_refuses_or_cannot_be_reached(client, check_calls, answer, flash):
    check_calls[0] = answer
    save_key(client)
    assert flash in flashes(client.get("/keys").text)
    assert env_file().exists()  # saved either way: the user may fix the key's settings at Google later


def test_a_refused_key_stays_marked_until_google_accepts_it(client, check_calls):
    check_calls[0] = (False, "Billing is off for this key's Google Cloud project. Check it on the API keys page.")
    save_key(client)
    page = client.get("/keys").text
    assert "Google Maps key saved, but Google refused it: Billing is off for this key&#39;s Google Cloud project." \
        in flashes(page) and "Check it on the API keys page" not in page
    # The flash is gone on the next visit; the page still says it, with an amber pill and no green tick.
    page = client.get("/keys").text
    assert "Key refused</span>" in page and "Google refused this key: Billing is off" in page
    assert ">Key saved</span>" not in page and "Check again" in page
    saved = env_file().read_text(encoding="utf-8")
    assert "OPENBERRY_GOOGLE_PLACES_KEY_STATUS=" in saved and saved.count(GOOGLE_KEY[-8:]) == 1  # only the key line
    # Fixed at Google: Check again clears it.
    check_calls[0] = (True, "")
    post(client, "/keys/google-maps", {"action": "check_key"})
    page = client.get("/keys").text
    assert "Google Maps key checked. Google accepted it." in flashes(page)
    assert "Google refused this key" not in page and ">Key saved</span>" in page
    assert check_calls[1:] == [GOOGLE_KEY, GOOGLE_KEY]
    # Google can't be reached: the last answer stands.
    check_calls[0] = (False, "Google says the API key is not valid.")
    post(client, "/keys/google-maps", {"action": "check_key"})
    check_calls[0] = (None, "ConnectError")
    post(client, "/keys/google-maps", {"action": "check_key"})
    assert "Google refused this key: Google says the API key is not valid." in client.get("/keys").text
    # A refusal is about one key: another key, or no key, is not marked.
    check_calls[0] = (None, "ConnectError")
    save_key(client, GOOGLE_KEY[:-4] + "zzzz")
    assert "Google refused this key" not in client.get("/keys").text
    post(client, "/keys/google-maps", {"action": "remove_key"})
    assert "STATUS" not in env_file().read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["", "short", "AIza with spaces in it 0123456789", "AIza;DROP TABLE x;--0123456789",
                                 "x" * 201])
def test_malformed_keys_are_refused(client, check_calls, bad: str):
    save_key(client, bad)
    assert "Paste the whole key: letters, digits, - and _ only." in flashes(client.get("/keys").text)
    assert not env_file().exists() and check_calls[1:] == []


def test_remove_the_key(client, check_calls):
    save_key(client)
    resp = post(client, "/keys/google-maps", {"action": "remove_key"})
    assert resp.status_code == 303
    page = client.get("/keys").text
    assert "Google Maps key removed. Google Maps searches are off." in flashes(page)
    assert "No key yet." in page and KEY not in env_file().read_text(encoding="utf-8")
    assert client.app.state.settings.google_places_key == ""


@pytest.mark.parametrize("bad", ["-1", "1001", "abc", "", "2.5"])
def test_monthly_limit_validation(client, bad: str):
    post(client, "/keys/google-maps", {"action": "save_limit", "limit": bad})
    assert "Enter a whole number from 0 to 1,000." in flashes(client.get("/keys").text)
    assert not env_file().exists()


def test_save_the_monthly_limit(client):
    post(client, "/keys/google-maps", {"action": "save_limit", "limit": "250"})
    page = client.get("/keys").text
    assert "Monthly limit saved: 250 searches." in flashes(page) and "0 of 250</strong>" in page
    assert client.app.state.settings.google_places_monthly_limit == 250
    post(client, "/keys/google-maps", {"action": "save_limit", "limit": "0"})
    assert "Monthly limit saved: 0. Google Maps searches are off." in flashes(client.get("/keys").text)


def test_a_key_in_the_environment_is_read_only(client, monkeypatch, check_calls):
    monkeypatch.setenv(KEY, "env-key-0123456789abcdefghijklmnop")
    monkeypatch.setenv(LIMIT, "100")
    page = client.get("/keys").text
    assert "The key is set in the server's environment (<code>OPENBERRY_GOOGLE_PLACES_KEY</code>)." in page
    assert 'name="key"' not in page and 'name="limit"' not in page
    assert "env-key-0123456789" not in page
    save_key(client)
    assert "is set in the server&#39;s environment. Change it there." in flashes(client.get("/keys").text)
    assert not env_file().exists()


def test_unknown_actions_change_nothing(client):
    post(client, "/keys/google-maps", {"action": "show_key"})
    assert "Nothing was changed." in flashes(client.get("/keys").text)


# --------------------------------------------------------------------------------------
# The searches in the company profile
# --------------------------------------------------------------------------------------


def test_searches_field_and_usage_line_on_the_profile(client, company, web_settings):
    settings_page = client.get(f"/c/{company.id}/settings").text
    assert 'name="signals.places_queries"' in settings_page and "Google Maps searches" in settings_page
    assert "Add your Google Maps API key on the" in settings_page and 'href="/keys#google-maps"' in settings_page
    assert 'value="business_search"' not in settings_page  # not a "signal type to track"
    web_settings.google_places_key = GOOGLE_KEY
    repo.reserve_api_call(google_places.SERVICE, 900)
    assert "1 of 900 Google Maps searches used this month." in client.get(f"/c/{company.id}/settings").text
    assert 'name="signals.places_queries"' in client.get("/register").text


def test_searches_keep_their_commas_when_saved(client, company):
    values = {k: v for k, v in forms.company_to_values(company).items() if v not in ("", [])}
    values["signals.places_queries"] = "law firms in DIFC, Dubai\r\nhotels in Business Bay, Dubai\r\n"
    resp = client.post(f"/c/{company.id}/settings", data={**values, "csrf_token": token(client)},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert repo.get_company(company.id).signals.places_queries == ["law firms in DIFC, Dubai",
                                                                   "hotels in Business Bay, Dubai"]
    page = client.get(f"/c/{company.id}/settings").text
    assert "law firms in DIFC, Dubai\nhotels in Business Bay, Dubai</textarea>" in page


def test_too_many_searches_is_a_form_error(client, company):
    values = {k: v for k, v in forms.company_to_values(company).items() if v not in ("", [])}
    values["signals.places_queries"] = "\n".join(f"hotels in area {n}" for n in range(31))
    resp = client.post(f"/c/{company.id}/settings", data={**values, "csrf_token": token(client)})
    assert resp.status_code == 422 and "At most 30 Google Maps searches." in resp.text


def test_public_registration_never_takes_searches(client, web_settings):
    web_settings.password = "pw"
    web_settings.public_registration = True
    assert 'name="signals.places_queries"' not in client.get("/register").text
    data = CompanyIn(name="Visitor Co",
                     signals={"places_queries": ["hotels in Dubai"], "rss_feeds": ["https://x.ae/f"]})
    pending = pages.as_pending_review(data)
    assert pending.signals.places_queries == [] and pending.signals.rss_feeds == []
    assert "Google Maps searches" in pages.as_pending_review.__doc__


# --------------------------------------------------------------------------------------
# Dashboard, lead page, filters, help
# --------------------------------------------------------------------------------------


def test_sources_card(client, company, web_settings):
    repo.update_company(company.id, {"signals": {"places_queries": [QUERY]}})
    html = client.get(f"/c/{company.id}").text
    row = html[html.index("Google Maps businesses"):][:1200]
    assert "Needs setup" in row and 'href="/keys#google-maps"' in row
    assert "Needs Google Maps searches plus a Google Maps API key" in row
    web_settings.google_places_key = GOOGLE_KEY
    row = client.get(f"/c/{company.id}").text
    row = row[row.index("Google Maps businesses"):][:1200]
    assert "Enabled" in row and "0 of 900 searches used this month" in row


def test_lead_page_links_to_google_maps(client, company):
    maps = repo.maps_place_url(ACME)
    lead, _ = repo.upsert_lead(company.id, LeadIn(
        lead_company="Acme Events", website="https://www.acme-events.ae/", email="info@acme-events.ae",
        phone="+97145550101", location="Dubai", profile_url=maps, source="google_places",
        signals=[SignalIn(type="business_search", title=f"Found on Google Maps: “{QUERY}”", url=maps,
                          source="google_places", external_id=f"gp:{ACME}", strength=10)]))
    page = client.get(f"/c/{company.id}/leads/{lead.id}").text
    assert f'href="{maps}"' in page and ">Google Maps</a>" in page.replace("\n", "")
    assert "Matches a business search you set up" in page and "info@acme-events.ae" in page
    other, _ = repo.upsert_lead(company.id, LeadIn(full_name="Jo Hn",
                                                   profile_url="https://news.ycombinator.com/user?id=jo"))
    assert "Profile</a>" in client.get(f"/c/{company.id}/leads/{other.id}").text
    listing = client.get(f"/c/{company.id}/leads?source=google_places").text
    assert "Acme Events" in listing and "Jo Hn" not in listing
    assert '<option value="google_places" selected>Google Maps</option>' in listing
    signals = client.get(f"/c/{company.id}/signals?source=google_places").text
    assert "Found on Google Maps" in signals


def test_the_lead_sentence_follows_its_signals(client, company):
    manual, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Acme Events LLC"))
    assert "the company is showing intent" not in client.get(f"/c/{company.id}/leads/{manual.id}").text
    maps = repo.maps_place_url(ACME)
    found, _ = repo.upsert_lead(company.id, LeadIn(
        lead_company="Acme Events", profile_url=maps, source="google_places",
        signals=[SignalIn(type="business_search", title="Found on Google Maps", url=maps, source="google_places",
                          external_id=f"gp:{ACME}", strength=10)]))
    assert found.id == manual.id  # merged by name into the lead added by hand
    page = client.get(f"/c/{company.id}/leads/{manual.id}").text
    assert "a business that matches your Google Maps search" in page and "showing intent" not in page
    repo.add_signal(company.id, SignalIn(type="hiring", title="Hiring an events coordinator", source="jobs",
                                         strength=60), lead_id=manual.id)
    assert "the company is showing intent" in client.get(f"/c/{company.id}/leads/{manual.id}").text


def test_dashboard_keeps_google_maps_businesses_apart_from_intent(client, company):
    for n in range(3):
        place = f"ChIJBusiness{n:08d}"
        repo.upsert_lead(company.id, LeadIn(
            lead_company=f"Business {n}", profile_url=repo.maps_place_url(place), source="google_places",
            signals=[SignalIn(type="business_search", title=f"Found on Google Maps {n}", source="google_places",
                              external_id=f"gp:{place}", strength=10)]))
    repo.upsert_lead(company.id, LeadIn(lead_company="Hiring Co", signals=[
        SignalIn(type="hiring", title="Hiring a travel manager", source="jobs", strength=60)]))
    stats = repo.company_stats(company.id)
    assert (stats["signals_7d"], stats["businesses_7d"]) == (1, 3)
    assert sum(day["count"] for day in stats["signals_by_day"]) == 1
    html = client.get(f"/c/{company.id}").text
    recent = html[html.index('id="recent-title"'):html.index('id="followups-title"')]
    assert "Hiring a travel manager" in recent and "Found on Google Maps" not in recent
    assert "Also 3 businesses found on Google Maps in the last 7 days." in recent
    assert f'href="/c/{company.id}/leads?source=google_places"' in recent


def test_scan_history_says_what_google_maps_did(client, company):
    counts = {"searches": 3, "businesses": 20, "added": 15, "no_website": 2, "robots": 1, "unreachable": 1,
              "already_handled": 1, "searches_not_due": 4, "searches_waiting": 1}
    run = repo.start_scan_run(company.id, "manual")
    repo.finish_scan_run(run, "ok", {"collectors": {"google_places": {"found": 15, "warnings": [], "counts": counts}},
                                     "signals_new": 15, "leads_new": 15})
    html = client.get(f"/c/{company.id}").text
    assert ("Google Maps: 3 searches, 20 businesses found, 15 added. Skipped: 2 without a website, 1 blocked by "
            "robots.txt, 1 unreachable, 1 found before. 4 searches not due yet: each search runs at most once a "
            "week. 1 search waiting for the next scan.") in html
    assert pages.places_summary({"searches_not_due": 1}) == ("Google Maps: 1 search not due yet: each search runs at "
                                                             "most once a week.")
    assert pages.places_summary({}) == ""


def test_help_page_mentions_the_google_maps_key(client, web_settings):
    assert "Google Maps</strong> (not set up)" in client.get("/help").text
    web_settings.google_places_key = GOOGLE_KEY
    assert "Google Maps</strong> (set up)" in client.get("/help").text
