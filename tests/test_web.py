"""Web dashboard and JSON API tests (FastAPI TestClient against a temporary SQLite database)."""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
from pathlib import Path
import sys
import time
import types
from contextlib import asynccontextmanager
from datetime import timedelta
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openberry import repo, services, website
from openberry.models import CompanyIn, LeadIn, SignalIn
from openberry.web import charts, forms, pages, scans, session, ui
from openberry.web.app import create_app

CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)">')


@pytest.fixture
def web_settings(settings):
    settings.scheduler_enabled = False
    settings.http_mcp_enabled = False
    settings.allowed_hosts = ["testserver"]  # TestClient's Host header (see auth.host_allowed)
    return settings


@pytest.fixture
def client(web_settings):
    with TestClient(create_app(web_settings)) as c:
        yield c


def token(client: TestClient, path: str = "/companies") -> str:
    match = CSRF_META.search(client.get(path).text)
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def post(client: TestClient, path: str, data: dict[str, Any] | None = None, *, page: str = "/companies",
         **kwargs: Any):
    payload = dict(data or {})
    payload["csrf_token"] = token(client, page)
    return client.post(path, data=payload, follow_redirects=False, **kwargs)


def demo(client: TestClient) -> int:
    resp = post(client, "/demo")
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[-1])


def flat_values(company: CompanyIn) -> dict[str, Any]:
    """The registration form as the browser would submit it for this profile."""
    return {k: v for k, v in forms.company_to_values(company).items() if v not in ("", [])}


class _FormReader(HTMLParser):
    """What a browser submits for the form posting to `action`: checked boxes only, the selected
    option (else the first one), textarea contents, every named input."""

    def __init__(self, action: str) -> None:
        super().__init__(convert_charrefs=True)
        self.action, self.inside = action, False
        self.fields: dict[str, list[str]] = {}
        self._select: tuple[str, list[tuple[str, bool]]] | None = None
        self._textarea: tuple[str, list[str]] | None = None

    def _add(self, name: str, value: str) -> None:
        self.fields.setdefault(name, []).append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.inside = a.get("action") == self.action
        elif not self.inside or (tag in ("input", "select", "textarea") and "name" not in a):
            return
        elif tag == "input" and (a.get("type") not in ("checkbox", "radio") or "checked" in a):
            self._add(a["name"], a.get("value", ""))
        elif tag == "select":
            self._select = (a["name"], [])
        elif tag == "option" and self._select:
            self._select[1].append((a.get("value", ""), "selected" in a))
        elif tag == "textarea":
            self._textarea = (a["name"], [])

    def handle_data(self, data: str) -> None:
        if self._textarea:
            self._textarea[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.inside = False
        elif tag == "select" and self._select:
            name, options = self._select
            chosen = [v for v, selected in options if selected] or [v for v, _ in options[:1]]
            self._add(name, chosen[-1] if chosen else "")
            self._select = None
        elif tag == "textarea" and self._textarea:
            self._add(self._textarea[0], "".join(self._textarea[1]))
            self._textarea = None


def browser_submit(client: TestClient, page: str, action: str):
    """Load `page` and submit its form for `action` unchanged, as a browser would."""
    reader = _FormReader(action)
    reader.feed(client.get(page).text)
    assert reader.fields, f"no form posting to {action} on {page}"
    return client.post(action, data=reader.fields, follow_redirects=False)


# --------------------------------------------------------------------------------------
# Pages render
# --------------------------------------------------------------------------------------

def test_pages_render_on_empty_database(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "Register your company" in home.text and "Load demo data" in home.text
    for path in ("/companies", "/register", "/help"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "No companies registered yet" in resp.text or path != "/companies"
    assert client.get("/healthz").json() == {"ok": True, "app": "openberry"}
    missing = client.get("/c/999")
    assert missing.status_code == 404 and "Page not found" in missing.text
    assert client.get("/c/not-a-number").status_code == 404
    assert client.get("/static/app.css").status_code == 200


def test_every_page_renders_with_demo_data(client):
    company_id = demo(client)
    leads, _ = repo.list_leads(company_id, limit=100)
    person = next(lead for lead in leads if lead.kind == "person")
    account = next(lead for lead in leads if lead.kind == "account")
    assert client.get("/", follow_redirects=False).headers["location"] == f"/c/{company_id}"
    base = f"/c/{company_id}"
    for path in (base, f"{base}/leads", f"{base}/leads?page=2&sort=name", f"{base}/leads/new",
                 f"{base}/leads/{person.id}", f"{base}/leads/{account.id}", f"{base}/signals",
                 f"{base}/outreach", f"{base}/outreach?tab=approved", f"{base}/outreach?tab=sent",
                 f"{base}/outreach?tab=replies", f"{base}/outreach?tab=followups", f"{base}/settings",
                 "/companies", "/register", "/help"):
        resp = client.get(path)
        assert resp.status_code == 200, path
    dashboard = client.get(base).text
    assert "Hottest leads" in dashboard and "Use with Claude" in dashboard
    assert f"read company {company_id}&#39;s profile" in dashboard
    assert "Signals, last 14 days" in dashboard and "View as table" in dashboard
    board = client.get("/companies").text
    assert "Demo: Desert Line Chauffeurs" in board and "50 qualified corporate leads" in board
    account_page = client.get(f"{base}/leads/{account.id}").text
    assert "Ask Claude to find the decision-maker" in account_page


def test_company_switcher_and_unknown_switch(client, company):
    assert client.get(f"/switch?company={company.id}", follow_redirects=False).headers["location"] == f"/c/{company.id}"
    assert client.get("/switch?company=abc", follow_redirects=False).headers["location"] == "/companies"


# --------------------------------------------------------------------------------------
# Registration board
# --------------------------------------------------------------------------------------

def test_registration_creates_company_with_nested_fields(client):
    resp = post(client, "/register", {
        "website": "acme-limo.example",
        "name": "Acme Limo",
        "industry": "Chauffeur services",
        "company_size": "11-50",
        "competitors": "Blacklane, Careem\nUber",
        "best_customers": "bigbank.example",
        "icp.job_titles": "Travel Manager\nExecutive Assistant",
        "icp.seniorities": ["manager", "director"],
        "icp.company_sizes": ["51-200", "201-1000"],
        "icp.company_types": ["enterprise"],
        "icp.locations": "UAE, KSA",
        "signals.enabled_types": ["hiring", "keyword_mention"],
        "signals.keywords": "chauffeur\ncorporate travel",
        "signals.subreddits": "r/dubai",
        "signals.github_repos": "https://github.com/acme/limo-sdk",
        "signals.job_boards": "greenhouse:acmebank:Acme Bank\nlever:northwind",
        "signals.lookback_days": "21",
        "scan_interval_hours": "12",
        "leads_per_week": "75",
        "requirements": "75 leads a week in the GCC",
        "outreach.tone": "direct",
        "outreach.channels": ["email"],
        "outreach.followup_days": "2, 5",
        "outreach.max_followups": "3",
        "outreach.mode": "auto_draft",
        "outreach.banned_words": "synergy",
        "notify.slack_webhook_url": "https://hooks.slack.com/services/x",
        "notify.min_score": "80",
    })
    assert resp.status_code == 303
    company_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    c = repo.get_company(company_id)
    assert c.name == "Acme Limo" and c.website == "https://acme-limo.example"
    assert c.competitors == ["Blacklane", "Careem", "Uber"]
    assert c.icp.job_titles == ["Travel Manager", "Executive Assistant"]
    assert c.icp.seniorities == ["manager", "director"] and c.icp.company_sizes == ["51-200", "201-1000"]
    assert c.icp.locations == ["UAE", "KSA"] and c.icp.company_types == ["enterprise"]
    assert c.signals.enabled_types == ["hiring", "keyword_mention"]
    assert c.signals.subreddits == ["dubai"] and c.signals.github_repos == ["acme/limo-sdk"]
    assert [(b.provider, b.token, b.company) for b in c.signals.job_boards] == [
        ("greenhouse", "acmebank", "Acme Bank"), ("lever", "northwind", "northwind")]
    assert (c.signals.lookback_days, c.scan_interval_hours, c.leads_per_week) == (21, 12, 75)
    assert c.outreach.channels == ["email"] and c.outreach.followup_days == [2, 5]
    assert c.outreach.mode == "auto_draft" and c.outreach.max_followups == 3 and c.outreach.tone == "direct"
    assert c.notify.min_score == 80 and c.notify.slack_webhook_url.startswith("https://")
    page = client.get(resp.headers["location"])
    assert "Registered! Next: run your first scan or connect Claude" in page.text


def test_registration_validation_errors_rerender_with_input(client):
    resp = post(client, "/register", {
        "name": "",
        "description": "keep this text <b>please</b>",
        "icp.seniorities": ["vp"],
        "signals.job_boards": "greenhouse:ok:Ok\nmonster:acme",
        "signals.lookback_days": "500",
        "notify.slack_webhook_url": "http://insecure.example",
        "outreach.followup_days": "3, soon",
    })
    assert resp.status_code == 422
    html = resp.text
    assert "keep this text &lt;b&gt;please&lt;/b&gt;" in html
    assert re.search(r'value="vp" checked', html)
    assert "greenhouse:ok:Ok\nmonster:acme" in html
    assert "Line 2: provider must be one of greenhouse, lever, ashby." in html
    assert "Use whole numbers of days" in html
    assert repo.list_companies() == []
    # After the pre-checks pass, pydantic errors are mapped to their fields.
    resp = post(client, "/register", {"name": "", "signals.lookback_days": "500",
                                      "notify.slack_webhook_url": "http://insecure.example"})
    assert resp.status_code == 422
    assert "Company name is required." in resp.text
    assert "Input should be less than or equal to 90." in resp.text
    assert "webhook URLs must start with https://." in resp.text
    assert 'data-first-step="company"' in resp.text


def test_form_values_roundtrip(company):
    values = forms.company_to_values(company)
    rebuilt, errors = forms.build_company(values, keep=company)
    assert errors == {} and rebuilt is not None
    assert rebuilt.model_dump() == CompanyIn.model_validate(company.model_dump()).model_dump()


def test_settings_edit_keeps_hidden_fields(client, company):
    repo.update_company(company.id, {"status": "paused", "signals": {"weights": {"hiring": 90}}})
    page = client.get(f"/c/{company.id}/settings")
    assert page.status_code == 200
    assert 'value="Acme Chauffeurs"' in page.text and "greenhouse:acmebank:Acme Bank" in page.text
    values = flat_values(repo.get_company(company.id))
    values["icp.locations"] = "KSA\nQatar"
    values["requirements"] = "Updated requirements"
    resp = post(client, f"/c/{company.id}/settings", values)
    assert resp.status_code == 303
    updated = repo.get_company(company.id)
    assert updated.icp.locations == ["KSA", "Qatar"] and updated.requirements == "Updated requirements"
    assert updated.status == "paused" and updated.signals.weights == {"hiring": 90}
    assert updated.signals.job_boards == company.signals.job_boards
    bad = post(client, f"/c/{company.id}/settings", {**values, "name": ""})
    assert bad.status_code == 422 and "Company name is required." in bad.text
    assert repo.get_company(company.id).name == "Acme Chauffeurs"


def _profile(company: CompanyIn | int) -> dict[str, Any]:
    """The profile as plain data; checkbox groups (sets to every consumer) come back in option order."""
    company = repo.get_company(company) if isinstance(company, int) else company
    data = CompanyIn.model_validate(company.model_dump()).model_dump()
    for f in forms.FIELDS:
        if f.kind == "checks":
            group, key = f.name.split(".")
            data[group][key] = sorted(data[group][key], key=str.lower)
    return data


def test_saving_the_profile_unchanged_keeps_every_value(client, company):
    # A browser submits only checked boxes and an existing <option>; values set through the API or
    # by Claude that the form has no option for must survive an unrelated save.
    resp = browser_submit(client, f"/c/{company.id}/settings", f"/c/{company.id}/settings")
    assert resp.status_code == 303
    assert _profile(company.id) == _profile(company)

    odd = repo.update_company(company.id, {
        "company_size": "about 30", "outreach": {"tone": "formal", "channels": ["LinkedIn", "Fax"]},
        "icp": {"seniorities": ["VP", "owner"], "company_sizes": ["50-200"], "company_types": ["scaleup"]},
        "signals": {"enabled_types": ["hiring", "Funding"]},
    })
    before = _profile(company.id)
    page = client.get(f"/c/{company.id}/settings").text
    assert re.search(r'<option value="about 30" selected>', page)
    assert re.search(r'value="owner" checked', page) and re.search(r'value="vp" checked', page)
    resp = browser_submit(client, f"/c/{company.id}/settings", f"/c/{company.id}/settings")
    assert resp.status_code == 303
    after = repo.get_company(company.id)
    assert (after.company_size, after.outreach.tone) == ("about 30", "formal")
    assert after.icp.company_sizes == ["50-200"] and after.icp.company_types == ["scaleup"]
    # Case variants of a known option are saved as that option.
    assert after.outreach.channels == ["linkedin", "Fax"] and after.icp.seniorities == ["vp", "owner"]
    assert after.signals.enabled_types == ["hiring", "funding"]
    assert odd.updated_at <= after.updated_at
    expected = {**before, "outreach": {**before["outreach"], "channels": ["Fax", "linkedin"]},
                "icp": {**before["icp"], "seniorities": ["owner", "vp"]},
                "signals": {**before["signals"], "enabled_types": ["funding", "hiring"]}}
    assert _profile(company.id) == expected


def test_form_option_helpers():
    opts = [("a", "A"), ("b", "B")]
    assert ui.select_options(opts, "b") == [("a", "A", False), ("b", "B", True)]
    assert ui.select_options(opts, "B") == [("a", "A", False), ("b", "B", True)]
    assert ui.select_options(opts, "") == [("a", "A", False), ("b", "B", False)]
    assert ui.select_options(opts, "zz") == [("a", "A", False), ("b", "B", False), ("zz", "zz", True)]
    assert ui.check_options(opts, ["B", "other"]) == [("a", "A", False), ("b", "B", True), ("other", "other", True)]
    assert ui.check_options(opts, None) == [("a", "A", False), ("b", "B", False)]


def test_every_signal_source_has_a_label():
    from openberry.models import SIGNAL_SOURCES
    assert set(SIGNAL_SOURCES) <= set(ui.SOURCE_LABELS)
    assert ui.templates.env.filters["source_label"]("sec_edgar") == "SEC EDGAR"


def test_pause_activate_and_delete_company(client, company):
    assert post(client, f"/c/{company.id}/status").status_code == 303
    assert repo.get_company(company.id).status == "paused"
    post(client, f"/c/{company.id}/status")
    assert repo.get_company(company.id).status == "active"
    resp = post(client, f"/c/{company.id}/delete")
    assert resp.status_code == 303 and resp.headers["location"] == "/companies"
    assert repo.find_company(company.id) is None


# --------------------------------------------------------------------------------------
# Leads
# --------------------------------------------------------------------------------------

def test_lead_add_update_and_delete(client, company):
    base = f"/c/{company.id}"
    bad = post(client, f"{base}/leads", {"title": "CEO"})
    assert bad.status_code == 422 and "account-level lead" in bad.text
    resp = post(client, f"{base}/leads", {
        "full_name": "Nadia Karim", "title": "Travel Manager", "lead_company": "Gulf Bank",
        "location": "Dubai, UAE", "linkedin_url": "https://www.linkedin.com/in/nadia-k",
        "tags": "vip, gcc", "signal_type": "keyword_mention", "signal_title": "Asked for chauffeur tips",
        "signal_url": "https://reddit.example/post", "signal_date": "2026-10-01", "signal_strength": "70",
    })
    assert resp.status_code == 303
    lead_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    lead = repo.get_lead(lead_id)
    assert lead.full_name == "Nadia Karim" and lead.tags == ["vip", "gcc"] and lead.intent_score > 0
    signals, total = repo.list_signals(company.id, lead_id=lead_id)
    assert total == 1 and signals[0].strength == 70 and signals[0].type == "keyword_mention"

    page = client.get(f"{base}/leads/{lead_id}")
    assert page.status_code == 200 and "Asked for chauffeur tips" in page.text

    post(client, f"{base}/leads/{lead_id}/update", {"status": "qualified", "notes": "Warm intro", "tags": "a, b"})
    lead = repo.get_lead(lead_id)
    assert (lead.status, lead.notes, lead.tags) == ("qualified", "Warm intro", ["a", "b"])
    post(client, f"{base}/leads/{lead_id}/update", {"status": "bogus"})
    assert repo.get_lead(lead_id).status == "qualified"

    post(client, f"{base}/leads/{lead_id}/profile", {"full_name": "Nadia Karim", "title": "Head of Travel",
                                                    "email": "nadia@gulfbank.example"})
    lead = repo.get_lead(lead_id)
    assert lead.title == "Head of Travel" and lead.email == "nadia@gulfbank.example"

    post(client, f"{base}/leads/{lead_id}/signals", {"signal_type": "funding", "signal_title": "Raised $5M"})
    assert repo.list_signals(company.id, lead_id=lead_id)[1] == 2

    resp = post(client, f"{base}/leads/{lead_id}/delete")
    assert resp.status_code == 303 and repo.find_lead(lead_id) is None


def test_lead_from_another_company_is_404(client, company):
    other = repo.create_company(CompanyIn(name="Other Co"))
    lead, _ = repo.upsert_lead(other.id, LeadIn(full_name="Not Yours"))
    assert client.get(f"/c/{company.id}/leads/{lead.id}").status_code == 404
    assert post(client, f"/c/{company.id}/leads/{lead.id}/delete").status_code == 404
    assert repo.find_lead(lead.id) is not None


def test_account_lead_edits_company_fields_and_links_to_add_a_person(client, company):
    account, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Harbor Lane Bank", company_domain="harbor.example"))
    person, _ = repo.upsert_lead(company.id, LeadIn(full_name="Lina Saab", lead_company="Elsewhere"))
    base = f"/c/{company.id}"
    page = client.get(f"{base}/leads/{account.id}").text
    # A name or title typed on an account lead would be stored but never shown.
    assert 'id="p-full_name"' not in page and 'id="p-title"' not in page and 'id="p-lead_company"' in page
    link = f"{base}/leads/new?lead_company=Harbor%20Lane%20Bank&amp;company_domain=harbor.example"
    assert link in page
    form = client.get(html_lib.unescape(link)).text
    assert 'value="Harbor Lane Bank"' in form and 'value="harbor.example"' in form
    assert 'id="p-full_name"' in client.get(f"{base}/leads/{person.id}").text
    resp = browser_submit(client, f"{base}/leads/{account.id}", f"{base}/leads/{account.id}/profile")
    assert resp.status_code == 303 and repo.get_lead(account.id).kind == "account"


def test_draft_approve_send_and_reply_flow(client, company, monkeypatch):
    lead, _ = repo.upsert_lead(company.id, LeadIn(
        full_name="Omar Haddad", title="Travel Manager", lead_company="Northwind",
        linkedin_url="https://www.linkedin.com/in/omar-h",
        signals=[SignalIn(type="competitor_engagement", title="Blacklane vs local chauffeurs")]))
    base = f"/c/{company.id}"
    resp = post(client, f"{base}/leads/{lead.id}/draft", {"channel": "linkedin_connect", "step": "1",
                                                          "engine": "template"})
    assert resp.status_code == 303
    [msg] = repo.list_messages(company.id, lead_id=lead.id)
    assert msg.status == "draft" and msg.channel == "linkedin_connect" and "Omar" in msg.body
    drafts = client.get(f"{base}/outreach").text
    assert "Omar Haddad" in drafts and "/ 200 characters" in drafts  # a free LinkedIn account

    resp = post(client, f"{base}/messages/{msg.id}", {"action": "approve", "next": f"{base}/outreach?tab=drafts"})
    assert resp.headers["location"] == f"{base}/outreach?tab=drafts"
    assert repo.get_message(msg.id).status == "approved"
    assert "Omar Haddad" in client.get(f"{base}/outreach?tab=approved").text

    post(client, f"{base}/messages/{msg.id}", {"action": "sent", "body": "Hi Omar, edited by hand."})
    sent = repo.get_message(msg.id)
    assert sent.status == "sent" and sent.body == "Hi Omar, edited by hand." and sent.sent_at is not None
    assert repo.get_lead(lead.id).status == "contacted"
    assert "edited by hand" in client.get(f"{base}/outreach?tab=sent").text

    post(client, f"{base}/leads/{lead.id}/reply", {"body": "Sounds good, call me Tuesday", "channel": "linkedin_dm"})
    assert repo.get_lead(lead.id).status == "replied"
    assert "call me Tuesday" in client.get(f"{base}/outreach?tab=replies").text
    assert "call me Tuesday" in client.get(f"{base}/leads/{lead.id}").text

    # Local AI drafting: a configuration error is shown, a working model is used.
    resp = post(client, f"{base}/leads/{lead.id}/draft", {"engine": "ollama", "channel": "email"})
    assert len(repo.list_messages(company.id, lead_id=lead.id, direction="outbound")) == 1
    assert "Local AI draft failed" in client.get(resp.headers["location"]).text

    async def fake_ollama(settings, context):
        assert context["lead"]["id"] == lead.id and context["channel"] == "email"
        return "Quick idea", "Body from the local model"

    from openberry import outreach
    monkeypatch.setattr(outreach, "draft_with_ollama", fake_ollama)
    post(client, f"{base}/leads/{lead.id}/draft", {"engine": "ollama", "channel": "email", "step": "2"})
    latest = repo.list_messages(company.id, lead_id=lead.id, direction="outbound")[0]
    assert (latest.subject, latest.body, latest.generated_by, latest.step) == (
        "Quick idea", "Body from the local model", "ollama", 2)

    post(client, f"{base}/messages/{latest.id}", {"action": "delete"})
    with pytest.raises(repo.NotFound):
        repo.get_message(latest.id)


def test_unexpected_ollama_errors_are_flashed(client, company, monkeypatch):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Omar Haddad"))

    async def broken_ollama(settings, context):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")  # e.g. a proxy's HTML error page

    from openberry import outreach
    monkeypatch.setattr(outreach, "draft_with_ollama", broken_ollama)
    resp = post(client, f"/c/{company.id}/leads/{lead.id}/draft", {"engine": "ollama"})
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert page.status_code == 200 and "Local AI draft failed: Expecting value" in page.text
    assert repo.list_messages(company.id, lead_id=lead.id) == []


def test_message_of_another_company_is_404(client, company):
    other = repo.create_company(CompanyIn(name="Other Co"))
    lead, _ = repo.upsert_lead(other.id, LeadIn(full_name="Someone"))
    msg = repo.create_message(lead.id, "hello")
    assert post(client, f"/c/{company.id}/messages/{msg.id}", {"action": "sent"}).status_code == 404
    assert repo.get_message(msg.id).status == "draft"


def test_csv_import_and_export(client, company):
    base = f"/c/{company.id}"
    csv_text = "First Name,Last Name,Title,Company,Location\nJane,Doe,Travel Manager,Acme Bank,Dubai\n,,,,\n"
    resp = post(client, f"{base}/leads/import", files={"file": ("leads.csv", csv_text.encode(), "text/csv")})
    assert resp.status_code == 303
    page = client.get(f"{base}/leads").text
    assert "Imported leads.csv: 1 new, 0 merged into existing leads, 1 skipped." in page
    assert "Jane Doe" in page
    repo.upsert_lead(company.id, LeadIn(full_name="Other Person", lead_company="Elsewhere"))
    export = client.get(f"{base}/leads.csv?q=Jane")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert export.headers["content-disposition"].startswith('attachment; filename="openberry-leads-acme-chauffeurs-')
    assert "Jane Doe" in export.text and "Other Person" not in export.text
    bad = post(client, f"{base}/leads/import", files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")})
    assert "Import failed: no recognisable columns" in client.get(bad.headers["location"]).text


def test_leads_signals_and_outreach_filters(client):
    company_id = demo(client)
    base = f"/c/{company_id}"
    hot = client.get(f"{base}/leads?tier=hot").text
    assert "Aisha Rahman" in hot and "Yousef Karim" not in hot
    accounts = client.get(f"{base}/leads?kind=account").text
    assert "Harbor Lane Bank" in accounts and "Aisha Rahman" not in accounts
    search = client.get(f"{base}/leads?q=Marina").text
    assert "Fatima Al Zaabi" in search and "Omar Haddad" not in search
    nothing = client.get(f"{base}/leads?q=zzzz-no-match").text
    assert "No leads match these filters" in nothing
    assert client.get(f"{base}/leads?tier=bogus&sort=evil&page=-3").status_code == 200
    hiring = client.get(f"{base}/signals?type=hiring").text
    assert "Hiring: Travel Manager, Middle East" in hiring and "GITEX Global" not in hiring
    none = client.get(f"{base}/signals?source=github").text
    assert "No signals match these filters" in none
    drafts = client.get(f"{base}/outreach?tab=drafts").text
    assert drafts.count('class="card queue-item"') == 2
    assert "No replies logged" in client.get(f"{base}/outreach?tab=replies").text


def test_user_content_is_escaped(client, company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(
        full_name="<script>alert(1)</script>", title="<img src=x onerror=alert(2)>", lead_company="Evil Corp",
        linkedin_url="javascript:alert(3)",
        signals=[SignalIn(type="custom", title="<b>bold</b>", url="javascript:alert(4)")]))
    for path in (f"/c/{company.id}/leads", f"/c/{company.id}/leads/{lead.id}", f"/c/{company.id}/signals",
                 f"/c/{company.id}"):
        html = client.get(path).text
        assert "<script>alert(1)</script>" not in html, path
        assert "<img src=x" not in html and "<b>bold</b>" not in html, path
        assert 'href="javascript:' not in html, path
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in client.get(f"/c/{company.id}/leads").text


# --------------------------------------------------------------------------------------
# Auth, CSRF, API
# --------------------------------------------------------------------------------------

def test_login_required_when_password_set(client, web_settings, company):
    web_settings.password = "s3cret"
    resp = client.get("/companies", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/companies"
    assert client.get(f"/c/{company.id}/leads", follow_redirects=False).status_code == 303
    assert client.get("/register", follow_redirects=False).status_code == 303
    assert client.get("/healthz").status_code == 200 and client.get("/static/app.js").status_code == 200
    login = client.get("/login?next=/companies")
    assert login.status_code == 200 and "Acme Chauffeurs" not in login.text
    wrong = post(client, "/login", {"password": "nope", "next": "/companies"}, page="/login")
    assert wrong.status_code == 401 and "That password is not right." in wrong.text
    ok = post(client, "/login", {"password": "s3cret", "next": "/companies"}, page="/login")
    assert ok.status_code == 303 and ok.headers["location"] == "/companies"
    assert client.get("/companies").status_code == 200
    evil = post(client, "/login", {"password": "s3cret", "next": "//evil.example"}, page="/login")
    assert evil.headers["location"] == "/"
    assert post(client, "/logout").status_code == 303
    assert client.get("/companies", follow_redirects=False).status_code == 303


def test_changing_the_password_logs_sessions_out(client, web_settings):
    web_settings.password = "first"
    post(client, "/login", {"password": "first"}, page="/login")
    assert client.get("/companies").status_code == 200
    web_settings.password = "second"
    assert client.get("/companies", follow_redirects=False).status_code == 303


def test_csrf_token_is_required_on_posts(client, company):
    assert client.post("/register", data={"name": "No Token"}).status_code == 403
    assert client.post("/register", data={"name": "Bad", "csrf_token": "forged"}).status_code == 403
    assert client.post(f"/c/{company.id}/delete", data={"csrf_token": ""}).status_code == 403
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"]
    page = client.post("/register", data={"name": "No Token"})
    assert "This form has expired" in page.text
    assert post(client, "/register", {"name": "With Token"}).status_code == 303


def test_api_local_mode_is_open(client, company):
    resp = client.get("/api/companies")
    assert resp.status_code == 200 and resp.json()["items"][0]["name"] == "Acme Chauffeurs"


def test_api_requires_bearer_token_in_server_mode(client, web_settings, company):
    web_settings.password = "pw"
    assert client.get("/api/companies").status_code == 401
    detail = client.get("/api/companies").json()["detail"]
    assert "OPENBERRY_API_TOKEN" in detail
    web_settings.api_token = "tok-123"
    assert client.get("/api/companies").status_code == 401
    assert client.get("/api/companies", headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": "Bearer tok-123"}
    assert client.get("/api/companies", headers=auth).status_code == 200
    # The dashboard's own fetch() calls use the session plus the CSRF header.
    csrf = token(client, "/login")
    assert client.get(f"/api/companies/{company.id}/scan-status", headers={"X-CSRF-Token": csrf}).status_code == 401
    post(client, "/login", {"password": "pw"}, page="/login")
    csrf = token(client)
    assert client.get(f"/api/companies/{company.id}/scan-status", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get(f"/api/companies/{company.id}/scan-status").status_code == 401


def test_api_crud(client, web_settings):
    web_settings.api_token = "tok"
    auth = {"Authorization": "Bearer tok"}
    resp = client.post("/api/companies", json={"name": "API Co", "icp": {"job_titles": "CTO, VP Engineering"}},
                       headers=auth)
    assert resp.status_code == 201
    company_id = resp.json()["id"]
    assert resp.json()["icp"]["job_titles"] == ["CTO", "VP Engineering"]
    assert client.post("/api/companies", json={"name": ""}, headers=auth).status_code == 422
    patched = client.patch(f"/api/companies/{company_id}", json={"icp": {"locations": ["UAE"]}}, headers=auth)
    assert patched.status_code == 200 and patched.json()["icp"]["job_titles"] == ["CTO", "VP Engineering"]
    assert patched.json()["icp"]["locations"] == ["UAE"]
    assert client.patch(f"/api/companies/{company_id}", json={"status": "gone"}, headers=auth).status_code == 422

    lead = {"full_name": "Ada L", "title": "CTO", "lead_company": "Engines Ltd",
            "signals": [{"type": "hiring", "title": "Hiring engineers"}]}
    created = client.post(f"/api/companies/{company_id}/leads", json=lead, headers=auth)
    assert created.status_code == 201 and created.json()["created"] is True
    lead_id = created.json()["lead"]["id"]
    again = client.post(f"/api/companies/{company_id}/leads", json=lead, headers=auth)
    assert again.status_code == 200 and again.json()["created"] is False
    listed = client.get(f"/api/companies/{company_id}/leads?search=Ada&limit=5", headers=auth).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == lead_id
    detail = client.get(f"/api/leads/{lead_id}", headers=auth).json()
    assert detail["full_name"] == "Ada L" and detail["signals"][0]["title"] == "Hiring engineers"
    updated = client.patch(f"/api/leads/{lead_id}", json={"status": "qualified"}, headers=auth)
    assert updated.json()["status"] == "qualified"
    bad = client.patch(f"/api/leads/{lead_id}", json={"status": "bogus"}, headers=auth)
    assert bad.status_code == 422 and "status must be one of" in bad.json()["detail"]
    assert client.patch(f"/api/leads/{lead_id}", json={"score": 99}, headers=auth).status_code == 422
    signals = client.get(f"/api/companies/{company_id}/signals?type=hiring", headers=auth).json()
    assert signals["total"] == 1
    stats = client.get(f"/api/companies/{company_id}/stats", headers=auth).json()
    assert stats["people"] == 1 and len(stats["signals_by_day"]) == 14
    missing = client.get("/api/leads/999999", headers=auth)
    assert missing.status_code == 404 and "not found" in missing.json()["detail"]
    assert client.get("/api/companies/999999/leads", headers=auth).status_code == 404
    assert client.get("/api/openapi.json").status_code == 200


def test_public_registration_mode(client, web_settings, monkeypatch):
    web_settings.password = "pw"
    web_settings.public_registration = True
    repo.create_company(CompanyIn(name="Secret Client Corp"))
    page = client.get("/register")
    assert page.status_code == 200
    assert "Secret Client Corp" not in page.text and 'class="sidebar"' not in page.text
    assert "Submit registration" in page.text
    resp = post(client, "/register", {"name": "Prospect Inc", "requirements": "Need 20 leads/week"}, page="/register")
    assert resp.status_code == 303 and resp.headers["location"] == "/register/thanks"
    thanks = client.get("/register/thanks")
    assert thanks.status_code == 200 and "Secret Client Corp" not in thanks.text
    assert any(c.name == "Prospect Inc" for c in repo.list_companies())
    # Bots that fill the hidden honeypot field get the thank-you page but nothing is stored.
    post(client, "/register", {"name": "Spam Bot", forms.HONEYPOT: "123"}, page="/register")
    assert not any(c.name == "Spam Bot" for c in repo.list_companies())
    # Anonymous visitors still can't read any data.
    assert client.get("/companies", follow_redirects=False).status_code == 303
    assert client.get("/api/companies", headers={"X-CSRF-Token": token(client, "/register")}).status_code == 401

    async def fake_fetch(url):
        return {"url": "https://prospect.example/", "site_name": "Prospect", "title": "Prospect | Home",
                "description": "We sell things", "headings": ["Sell more", "Feature one"], "text": ""}

    monkeypatch.setattr(website, "fetch_site_summary", fake_fetch)
    summary = client.post("/api/site-summary", json={"url": "prospect.example"},
                          headers={"X-CSRF-Token": token(client, "/register")})
    assert summary.status_code == 200 and summary.json()["suggestions"]["name"] == "Prospect"
    assert client.post("/api/site-summary", json={"url": "prospect.example"}).status_code == 401


def test_site_summary_errors(client, monkeypatch):
    resp = client.post("/api/site-summary", json={"url": "http://127.0.0.1/"})
    assert resp.status_code == 422 and "non-public" in resp.json()["detail"]

    async def fake_fetch(url):
        return {"url": url, "site_name": "Acme", "description": "Chauffeurs", "headings": ["Arrive calm"], "text": ""}

    monkeypatch.setattr(website, "fetch_site_summary", fake_fetch)
    data = client.post("/api/site-summary", json={"url": "acme.example"}).json()
    assert data["suggestions"]["value_proposition"] == "Arrive calm"


def test_security_headers(client):
    resp = client.get("/companies")
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert "<script>" not in resp.text  # all JavaScript is served from /static


def test_local_mode_refuses_dns_rebinding_hosts(client, web_settings, company):
    # Without a password, a page on an attacker's domain re-pointed at 127.0.0.1 would be same-origin.
    evil = {"Host": "rebind.evil.example:8000"}
    for path in ("/companies", "/api/companies", f"/c/{company.id}", "/register"):
        resp = client.get(path, headers=evil)
        assert resp.status_code == 400 and "OPENBERRY_ALLOWED_HOSTS" in resp.text, path
        assert "Acme" not in resp.text and resp.headers["x-content-type-options"] == "nosniff"
    assert post(client, f"/c/{company.id}/delete", headers=evil).status_code == 400
    assert repo.find_company(company.id) is not None
    for host in ("localhost:8000", "LOCALHOST", "127.0.0.1:8000", "[::1]:8000", "192.168.1.20:8000"):
        assert client.get("/api/companies", headers={"Host": host}).status_code == 200, host
    assert client.get("/healthz", headers=evil).status_code == 200
    web_settings.base_url = "https://leads.example.com"
    assert client.get("/api/companies", headers={"Host": "leads.example.com"}).status_code == 200
    web_settings.allowed_hosts = ["testserver", "openberry"]
    assert client.get("/api/companies", headers={"Host": "openberry:8000"}).status_code == 200
    web_settings.allowed_hosts = ["*"]
    assert client.get("/api/companies", headers=evil).status_code == 200
    # With a password the session cookie never reaches the attacker's origin, so no Host check.
    web_settings.allowed_hosts = ["testserver"]
    web_settings.password = "pw"
    assert client.get("/login", headers=evil).status_code == 200


def test_local_api_refuses_cross_site_writes(client, company, monkeypatch):
    async def fake_run_scan(company_id, **kwargs):
        return {"status": "ok"}

    monkeypatch.setattr(services, "run_scan", fake_run_scan)
    scan = f"/api/companies/{company.id}/scan"
    evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    # Another website can't make the visitor's browser start scans or plant data (a form POST needs no CORS).
    resp = client.post(scan, data={"x": "1"}, headers=evil)
    assert resp.status_code == 403 and "Cross-site request refused" in resp.json()["detail"]
    assert client.post(f"/api/companies/{company.id}/leads", json={"full_name": "Planted"},
                       headers=evil).status_code == 403
    assert repo.list_leads(company.id)[1] == 0 and not scans.task_running(company.id)
    assert client.get("/api/companies", headers=evil).status_code == 200  # CORS keeps the response from them
    # The dashboard's own calls carry the CSRF header; scripts send no Origin at all.
    own = client.post(scan, headers={"Origin": "http://testserver", "X-CSRF-Token": token(client)})
    assert own.status_code == 202
    wait_until(lambda: not scans.task_running(company.id))
    assert client.post(scan).status_code == 202


def test_api_validation_errors_are_422_json(client, company):
    resp = client.post("/api/companies", json={"name": "X", "notify": {"slack_webhook_url": "http://hooks.example"}})
    assert resp.status_code == 422
    [err] = resp.json()["detail"]
    assert err["loc"][-1] == "slack_webhook_url" and "https://" in err["msg"]
    form = client.post("/api/companies", content=b"name=X", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status_code == 422 and form.json()["detail"][0]["loc"] == ["body"]
    signal = client.post(f"/api/companies/{company.id}/leads",
                         json={"full_name": "A", "signals": [{"title": "t", "strength": 500}]})
    assert signal.status_code == 422
    assert repo.list_companies() == [repo.get_company(company.id)]


def test_oversized_request_bodies_are_refused(client, web_settings, company, monkeypatch):
    from openberry.web import auth
    monkeypatch.setattr(auth, "MAX_BODY_BYTES", 2000)
    web_settings.password = "pw"  # /login takes anonymous POSTs, and uploads are spooled to disk
    resp = client.post("/login", files={"file": ("big.bin", b"x" * 5000)})
    assert resp.status_code == 413

    def chunks():  # no Content-Length: counted as it streams in
        yield b"password="
        for _ in range(5):
            yield b"x" * 1000

    resp = client.post("/login", content=chunks(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 413 and "That upload is too large" in resp.text
    assert post(client, "/login", {"password": "pw"}, page="/login").status_code == 303


def test_contacted_kpi_opens_every_reached_lead(client, company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Rana Aziz"))
    msg = repo.create_message(lead.id, "Hi Rana")
    repo.update_message(msg.id, status="sent")
    repo.log_reply(lead.id, "Interested!")
    [tile] = [k for k in pages.kpi_tiles(company.id, repo.company_stats(company.id)) if k["label"] == "Contacted"]
    assert tile["value"] == 1 and repo.get_lead(lead.id).status == "replied"
    assert "Rana Aziz" in client.get(tile["href"]).text  # leads?status=contacted would be empty


def test_session_cookie_is_secure_behind_https(web_settings):
    with TestClient(create_app(web_settings)) as c:
        assert "secure" not in c.get("/companies").headers["set-cookie"].lower()
    web_settings.base_url = "https://leads.example.com"
    with TestClient(create_app(web_settings), base_url="https://testserver") as c:
        assert "secure" in c.get("/companies").headers["set-cookie"].lower()


def test_long_flash_messages_keep_the_session_cookie_small(client, company):
    # Browsers drop cookies over 4 KB: the session (login, CSRF token) would be lost with it.
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Ñ" * 3000))
    resp = post(client, f"/c/{company.id}/leads/{lead.id}/delete")
    assert resp.status_code == 303 and len(resp.headers["set-cookie"]) < 4096
    page = client.get(resp.headers["location"]).text
    assert "Deleted ÑÑÑ" in page and "…" in page

    fake = types.SimpleNamespace(session={})
    for i in range(12):
        session.flash(fake, f"{i} " + "é" * 2000, "warning")
    flashes = fake.session[session.FLASH_KEY]
    assert flashes[-1][1].startswith("11 ") and len(flashes[-1][1]) == session.MAX_FLASH_CHARS
    assert len(json.dumps(flashes)) <= session.MAX_FLASH_BYTES


def test_help_snippets_survive_any_database_path(client, web_settings, tmp_path):
    # Windows paths (backslashes) and spaces must not break the copied JSON config or shell command.
    web_settings.db_path = tmp_path / "My Data" / 'odd\\name "x".db'
    page = client.get("/help").text
    config = re.search(r'<pre id="cfg-desktop">(.*?)</pre>', page, re.S).group(1)
    data = json.loads(html_lib.unescape(config))
    assert data["mcpServers"]["openberry"]["env"]["OPENBERRY_DB"] == str(web_settings.db_path.resolve())
    command = html_lib.unescape(re.search(r'<pre id="cmd-code">(.*?)</pre>', page, re.S).group(1))
    assert f'OPENBERRY_DB="{web_settings.db_path.resolve()}"' in command


# --------------------------------------------------------------------------------------
# Scans, lifespan
# --------------------------------------------------------------------------------------

def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_run_scan_in_background(client, company, monkeypatch):
    calls: list[tuple[int, str]] = []
    release = {"go": False}

    async def fake_run_scan(company_id, *, trigger="manual", sources=None, client=None):
        calls.append((company_id, trigger))
        while not release["go"]:
            await asyncio.sleep(0.01)
        return {"status": "ok", "signals_new": 3, "leads_new": 1}

    monkeypatch.setattr(services, "run_scan", fake_run_scan)
    resp = post(client, f"/c/{company.id}/scan")
    assert resp.status_code == 303 and resp.headers["location"] == f"/c/{company.id}"
    wait_until(lambda: calls)
    dashboard = client.get(f"/c/{company.id}").text
    assert "Scan started" in dashboard and "data-scan-poll" in dashboard
    assert client.get(f"/api/companies/{company.id}/scan-status").json()["running"] is True
    # A second scan for the same company is refused while one runs.
    post(client, f"/c/{company.id}/scan")
    assert "A scan is already running" in client.get(f"/c/{company.id}").text
    assert client.post(f"/api/companies/{company.id}/scan").status_code == 409
    assert len(calls) == 1
    release["go"] = True
    wait_until(lambda: not client.get(f"/api/companies/{company.id}/scan-status").json()["running"])
    status = client.get(f"/api/companies/{company.id}/scan-status").json()
    assert status["last_result"]["signals_new"] == 3 and calls == [(company.id, "dashboard")]
    assert "data-scan-poll" not in client.get(f"/c/{company.id}").text
    api = client.post(f"/api/companies/{company.id}/scan")
    assert api.status_code == 202 and api.json()["started"] is True
    wait_until(lambda: len(calls) == 2)
    assert calls[1] == (company.id, "api")
    assert client.post("/api/companies/999/scan").status_code == 404


def test_failed_scan_is_reported(client, company, monkeypatch):
    async def boom(company_id, **kwargs):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(services, "run_scan", boom)
    post(client, f"/c/{company.id}/scan")
    wait_until(lambda: not scans.task_running(company.id))
    assert "The last scan failed: RuntimeError: collector exploded" in client.get(f"/c/{company.id}").text


def test_scan_started_elsewhere_in_the_same_instant_is_shown_as_running(client, company, monkeypatch):
    async def lose_the_race(company_id, *, trigger="manual", sources=None, client=None):
        repo.start_scan_run(company_id, "schedule")  # the scheduler won the start
        repo.start_scan_run(company_id, trigger)  # raises ScanInProgress, like the real run_scan

    monkeypatch.setattr(services, "run_scan", lose_the_race)
    post(client, f"/c/{company.id}/scan")
    wait_until(lambda: not scans.task_running(company.id))
    current = scans.status(company.id)
    assert current["running"] is True and current["last_result"] is None
    assert "The last scan failed" not in client.get(f"/c/{company.id}").text


def test_scan_interrupted_by_shutdown_does_not_block_the_next_one(web_settings, company, monkeypatch):
    async def hanging_scan(company_id, *, trigger="manual", sources=None, client=None):
        repo.start_scan_run(company_id, trigger)
        await asyncio.sleep(3600)

    monkeypatch.setattr(services, "run_scan", hanging_scan)
    with TestClient(create_app(web_settings)) as c:
        post(c, f"/c/{company.id}/scan")
        wait_until(lambda: repo.list_scan_runs(company.id))
        assert scans.status(company.id)["running"] is True
    # Shutdown cancelled the scan; its run must not look 'running' to the restarted app.
    [run] = repo.list_scan_runs(company.id)
    assert run.status == "failed" and run.stats["errors"] == ["Interrupted: the server stopped during the scan."]
    assert scans.status(company.id)["running"] is False


def test_lifespan_runs_hooks_scheduler_and_mcp_mount(web_settings, monkeypatch):
    events: list[str] = []

    @asynccontextmanager
    async def hook():
        events.append("enter")
        yield
        events.append("exit")

    def mount_http(app, settings):
        app.state.lifespan_hooks.append(hook)

    async def fake_loop(stop):
        events.append("scheduler")
        await stop.wait()
        events.append("scheduler-stopped")

    from openberry import scheduler
    monkeypatch.setitem(sys.modules, "openberry.mcp_server", types.SimpleNamespace(mount_http=mount_http))
    monkeypatch.setattr(scheduler, "scheduler_loop", fake_loop)
    web_settings.http_mcp_enabled = True
    web_settings.scheduler_enabled = True
    with TestClient(create_app(web_settings)) as c:
        assert c.get("/healthz").status_code == 200
        wait_until(lambda: "scheduler" in events)
        assert events[0] == "enter"
    assert events == ["enter", "scheduler", "scheduler-stopped", "exit"]


def test_app_starts_without_mcp_module(web_settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "openberry.mcp_server", None)  # import raises ImportError
    web_settings.http_mcp_enabled = True
    with TestClient(create_app(web_settings)) as c:
        assert c.get("/healthz").json() == {"ok": True, "app": "openberry"}


# --------------------------------------------------------------------------------------
# Chart helpers
# --------------------------------------------------------------------------------------

def test_chart_scales_and_labels():
    assert charts.nice_scale(0) == (0, [0])
    assert charts.nice_scale(3) == (3, [0, 1, 2, 3])
    assert charts.nice_scale(7) == (8, [0, 2, 4, 6, 8])
    assert charts.nice_scale(130) == (150, [0, 50, 100, 150])
    assert charts.nice_scale(260) == (300, [0, 100, 200, 300])
    points = [{"date": f"2026-09-{d:02d}", "count": c} for d, c in ((29, 0), (30, 4), (1, 0))]
    points[2]["date"] = "2026-10-01"
    days = charts.day_columns(points)
    assert [c["xlabel"] for c in days["cols"]] == ["29 Sep", "30", "1 Oct"]
    assert [c["label_value"] for c in days["cols"]] == [False, True, False]
    assert days["cols"][1]["pct"] == 100 and days["cols"][2]["tip"] == "Today: 0 signals"
    rows = [{"type": f"t{i}", "label": f"T{i}", "count": 10 - i} for i in range(9)]
    bars = charts.type_bars(rows)
    assert len(bars["bars"]) == 7 and bars["bars"][-1]["label"] == "Other (3 types)"
    assert bars["bars"][0]["frac"] == 1


# --------------------------------------------------------------------------------------
# Public registration: review, rate limits, unique names; login throttling
# --------------------------------------------------------------------------------------

def test_public_registrations_wait_paused_without_visitor_chosen_urls(client, web_settings, monkeypatch):
    web_settings.password = "pw"
    web_settings.public_registration = True
    form = client.get("/register").text
    assert 'name="signals.rss_feeds"' not in form and 'name="notify.slack_webhook_url"' not in form
    resp = post(client, "/register", {
        "name": "Prospect Inc", "scan_interval_hours": "1", "signals.keywords": "chauffeur",
        "signals.rss_feeds": "https://attacker.example/feed.xml", "notify.min_score": "0",
        "notify.slack_webhook_url": "https://attacker.example/collect",
        "notify.discord_webhook_url": "https://attacker.example/discord",
    }, page="/register")
    assert resp.status_code == 303 and resp.headers["location"] == "/register/thanks"
    [stored] = repo.list_companies()
    assert stored.status == "paused" and stored.signals.rss_feeds == [] and stored.signals.keywords == ["chauffeur"]
    assert stored.notify.slack_webhook_url == "" and stored.notify.discord_webhook_url == ""
    assert repo.companies_due_for_scan() == []
    # The operator sees it as pending review on the board and activates it there.
    post(client, "/login", {"password": "pw"}, page="/login")
    board = client.get("/companies").text
    assert "Pending review" in board and "pill-company-pending" in board
    resp = post(client, f"/c/{stored.id}/status", {"status": "active", "next": "/companies"})
    assert resp.headers["location"] == "/companies" and repo.get_company(stored.id).status == "active"
    assert "Pending review" not in client.get("/companies").text
    # The explicit status is idempotent (a double click doesn't pause it again); no status still toggles.
    post(client, f"/c/{stored.id}/status", {"status": "active"})
    assert repo.get_company(stored.id).status == "active"
    later = repo.utcnow() + timedelta(minutes=5)  # timestamps have one-second resolution
    monkeypatch.setattr(repo, "utcnow", lambda: later)
    post(client, f"/c/{stored.id}/status")
    assert repo.get_company(stored.id).status == "paused"
    assert "Pending review" not in client.get("/companies").text  # reviewed since: just paused


def test_registration_refuses_a_duplicate_company_name(client, web_settings, company):
    resp = post(client, "/register", {"name": "  acme CHAUFFEURS "})
    assert resp.status_code == 422 and "already registered" in resp.text
    web_settings.password = "pw"
    web_settings.public_registration = True
    anon = TestClient(client.app)
    assert post(anon, "/register", {"name": "Acme Chauffeurs"}, page="/register").status_code == 422
    assert [c.name for c in repo.list_companies()] == ["Acme Chauffeurs"]


def test_anonymous_registrations_and_site_lookups_are_rate_limited(client, web_settings, monkeypatch):
    web_settings.password = "pw"
    web_settings.public_registration = True
    for i in range(5):
        assert post(client, "/register", {"name": f"Spam {i}"}, page="/register").status_code == 303
    blocked = post(client, "/register", {"name": "Spam 5", "description": "keep me"}, page="/register")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    assert "Too many registrations" in blocked.text and "keep me" in blocked.text
    assert len(repo.list_companies()) == 5
    other = TestClient(client.app, client=("203.0.113.9", 4000))  # another address has its own budget
    assert post(other, "/register", {"name": "Real Client"}, page="/register").status_code == 303

    async def fake_fetch(url):
        return {"url": url, "site_name": "Acme", "description": "", "headings": [], "text": ""}

    monkeypatch.setattr(website, "fetch_site_summary", fake_fetch)
    headers = {"X-CSRF-Token": token(client, "/register")}
    codes = [client.post("/api/site-summary", json={"url": "acme.example"}, headers=headers).status_code
             for _ in range(6)]
    assert codes == [200] * 5 + [429]
    # The operator and API scripts are not limited.
    web_settings.api_token = "tok"
    assert all(client.post("/api/site-summary", json={"url": "acme.example"},
                           headers={"Authorization": "Bearer tok"}).status_code == 200 for _ in range(6))


def test_rate_limit_windows_and_global_cap():
    from openberry.web.ratelimit import RateLimit

    limit = RateLimit(per_client=2, window=60, total=3)
    assert limit.allow("a", now=0) == 0 and limit.allow("a", now=1) == 0
    assert limit.allow("a", now=2) == pytest.approx(58)  # a's oldest event expires at 60
    assert limit.allow("b", now=3) == 0
    assert limit.allow("c", now=4) == pytest.approx(56)  # everyone together: 3 per window
    assert limit.allow("a", now=61) == 0 and limit.count(now=61) == 2  # b@3 and a@61 are left


def test_failed_logins_are_limited_per_client(client, web_settings, monkeypatch):
    from openberry.web import auth

    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY", 0)
    web_settings.password = "s3cret"
    for _ in range(5):
        assert post(client, "/login", {"password": "guess"}, page="/login").status_code == 401
    # Even the right password is refused for a while: guessing can't continue from this address.
    blocked = post(client, "/login", {"password": "s3cret"}, page="/login")
    assert blocked.status_code == 429 and "Too many wrong passwords" in blocked.text
    assert int(blocked.headers["retry-after"]) > 0
    assert client.get("/companies", follow_redirects=False).status_code == 303
    other = TestClient(client.app, client=("198.51.100.7", 4000))
    assert post(other, "/login", {"password": "s3cret"}, page="/login").status_code == 303


def test_failed_login_delay_grows_with_everyone_s_failures():
    from openberry.web import auth

    assert auth.failed_login_delay(0) == auth.FAILED_LOGIN_DELAY
    assert auth.failed_login_delay(5) == 2 * auth.FAILED_LOGIN_DELAY
    assert auth.failed_login_delay(10_000) == auth.MAX_FAILED_LOGIN_DELAY


def test_parallel_login_guesses_wait_for_each_other(web_settings, monkeypatch):
    import httpx

    from openberry.web import auth

    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY", 0.1)
    web_settings.password = "s3cret"
    app = create_app(web_settings)

    async def guesses() -> tuple[list[int], float]:
        transport = httpx.ASGITransport(app=app, client=("192.0.2.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            csrf = CSRF_META.search((await c.get("/login")).text).group(1)
            started = time.monotonic()
            responses = await asyncio.gather(*(c.post("/login", data={"password": f"guess{i}", "csrf_token": csrf})
                                               for i in range(4)))
            return [r.status_code for r in responses], time.monotonic() - started

    codes, elapsed = asyncio.run(guesses())
    assert codes == [401] * 4
    assert elapsed >= 0.35  # one after another; in parallel all four would answer after ~0.1 s


# --------------------------------------------------------------------------------------
# Connect Claude page
# --------------------------------------------------------------------------------------

def _help_blocks(page: str) -> dict[str, str]:
    return {pid: html_lib.unescape(m.group(1)) for pid in ("cmd-code", "cfg-desktop", "cmd-http", "curl-example")
            if (m := re.search(rf'<pre id="{pid}">(.*?)</pre>', page, re.S))}


def test_help_mcp_command_matches_how_openberry_is_installed(client, web_settings, monkeypatch, tmp_path):
    db_path = str(web_settings.db_path.resolve())
    monkeypatch.setattr(pages, "in_container", lambda: False)
    # A clone run with uv, as in the README: Claude Desktop needs uv's full path (it has no shell PATH).
    monkeypatch.setattr(pages, "source_checkout", lambda: tmp_path / "open berry")
    monkeypatch.setattr(pages.shutil, "which", lambda name: "/home/me/.local/bin/uv" if name == "uv" else None)
    blocks = _help_blocks(client.get("/help").text)
    assert blocks["cmd-code"] == (f'claude mcp add openberry -e OPENBERRY_DB="{db_path}" -- /home/me/.local/bin/uv '
                                  f'--directory "{tmp_path / "open berry"}" run openberry mcp')
    server = json.loads(blocks["cfg-desktop"])["mcpServers"]["openberry"]
    assert server == {"command": "/home/me/.local/bin/uv",
                      "args": ["--directory", str(tmp_path / "open berry"), "run", "openberry", "mcp"],
                      "env": {"OPENBERRY_DB": db_path}}
    # An installed package (pip/pipx): this server's own Python runs the module.
    monkeypatch.setattr(pages, "source_checkout", lambda: None)
    server = json.loads(_help_blocks(client.get("/help").text)["cfg-desktop"])["mcpServers"]["openberry"]
    assert server["command"] == sys.executable and server["args"] == ["-m", "openberry", "mcp"]
    # In the Docker image: run it inside the container; the host has neither the command nor /data.
    monkeypatch.setattr(pages, "in_container", lambda: True)
    page = client.get("/help").text
    blocks = _help_blocks(page)
    assert blocks["cmd-code"] == "claude mcp add openberry -- docker exec -i openberry openberry mcp"
    assert json.loads(blocks["cfg-desktop"])["mcpServers"]["openberry"] == {
        "command": "docker", "args": ["exec", "-i", "openberry", "openberry", "mcp"]}
    assert "OPENBERRY_DB" not in page.split('id="h-remote"')[0]


def test_this_checkout_is_detected_as_a_source_checkout():
    root = pages.source_checkout()
    assert root is not None and (root / "pyproject.toml").is_file() and (root / "src" / "openberry").is_dir()


def test_help_uses_the_address_the_page_was_opened_on(client, web_settings):
    web_settings.http_mcp_enabled = True  # read when the page renders; the app was built without the mount
    assert web_settings.base_url == "http://127.0.0.1:8000"
    other_port = TestClient(client.app, base_url="http://127.0.0.1:8977")
    page = other_port.get("/help").text
    blocks = _help_blocks(page)
    assert '"http://127.0.0.1:8977/mcp"' in blocks["cmd-http"]
    # Quoted, so zsh doesn't treat the "?" as a glob.
    assert blocks["curl-example"].startswith('curl -s "http://127.0.0.1:8977/api/companies/1/leads?tier=hot" ')
    assert "8000" not in blocks["cmd-http"] + blocks["curl-example"]
    assert "but <code>OPENBERRY_BASE_URL</code> is <code>http://127.0.0.1:8000</code>" in page
    same = TestClient(client.app, base_url="http://127.0.0.1:8000").get("/help").text
    assert "OPENBERRY_BASE_URL</code> is" not in same


# --------------------------------------------------------------------------------------
# Wizard errors, form copy, dashboard copy
# --------------------------------------------------------------------------------------

def test_registration_error_summary_names_the_step_and_links_to_real_fields(client):
    resp = post(client, "/register", {"name": "Acme", "signals.github_repos": "not a repo",
                                      "contact_email": "omar@falconline"})
    assert resp.status_code == 422
    html = resp.text
    assert re.search(r'<a href="#f-signals-github_repos" data-error-step="signals"><span class="error-where">'
                     r'Signals &amp; requirements:</span>', html)
    assert 'class="wizard-dot has-error" data-step-to="signals"' in html
    assert 'class="wizard-dot has-error" data-step-to="company"' in html
    assert 'class="wizard-dot" data-step-to="offer"' in html
    # Pydantic errors on checkbox and radio groups link to their fieldset.
    resp = post(client, "/register", {"name": "Acme", "outreach.mode": "spam_everyone"})
    assert resp.status_code == 422 and 'data-error-step="outreach"' in resp.text
    for target in re.findall(r'<div class="form-alert".*?</ul>', resp.text, re.S)[0].split('href="#')[1:]:
        assert f'id="{target.split(chr(34))[0]}"' in resp.text


def test_contact_email_field_rejects_what_the_server_rejects(client):
    page = client.get("/register").text
    tag = re.search(r'<input[^>]*id="f-contact_email"[^>]*>', page).group(0)
    pattern = html_lib.unescape(re.search(r'pattern="([^"]+)"', tag).group(1))
    assert re.fullmatch(pattern, "omar@falconline") is None  # the browser now stops it on step 1
    assert re.fullmatch(pattern, "omar@falconline.ae")
    assert forms.build_company({**forms.default_values(), "name": "A", "contact_email": "omar@falconline"})[1]


def test_form_copy_matches_what_alerts_and_scoring_do(client, company):
    settings_page = client.get(f"/c/{company.id}/settings").text
    tag = re.search(r'<input[^>]*id="f-notify-min_score"[^>]*>', settings_page).group(0)
    assert 'min="0"' in tag and 'max="100"' in tag
    assert "when it turns hot (70+) with at least this score" in settings_page
    assert "Guides Claude&#39;s prospecting; it doesn&#39;t change scores." in settings_page


def test_dashboard_says_whether_automatic_scans_run(client, web_settings, company):
    web_settings.scheduler_enabled = False  # read at render time; this app runs no scheduler either way
    assert "Automatic scans are off on this server" in client.get(f"/c/{company.id}").text
    web_settings.scheduler_enabled = True
    page = client.get(f"/c/{company.id}").text
    assert "Automatic scans run every 24h" in page and "Automatic scans are off" not in page


def test_scan_history_shows_any_status_and_why(client, company):
    for status, stats in (("nothing_configured", {"error": "No signal source is configured for this company"}),
                          ("mystery_state", {"warnings": [], "collectors": {"rss": {"warnings": ["feed timed out"]}}})):
        repo.finish_scan_run(repo.start_scan_run(company.id, "schedule"), status, stats)
    page = client.get(f"/c/{company.id}").text
    assert '<span class="pill pill-run-nothing_configured">No sources configured</span>' in page
    assert '<span class="pill pill-run-other">Mystery state</span>' in page
    assert "No signal source is configured for this company" in page and "rss: feed timed out" in page


def test_dashboard_scan_that_found_no_sources_is_not_reported_as_fine(client, company, monkeypatch):
    async def nothing(company_id, **kwargs):
        return {"status": "nothing_configured", "error": "No signal source is configured for this company.",
                "signals_new": 0, "leads_new": 0}

    monkeypatch.setattr(services, "run_scan", nothing)
    post(client, f"/c/{company.id}/scan")
    wait_until(lambda: not scans.task_running(company.id))
    assert scans.status(company.id)["last_result"]["ok"] is False
    page = client.get(f"/c/{company.id}").text
    assert "Last scan: No sources configured. No signal source is configured for this company." in page


def test_people_tile_counts_people_added_this_week():
    from openberry.seed import seed_demo

    stats = repo.company_stats(seed_demo())
    assert stats["accounts"] > 0 and stats["new_leads_7d"] == stats["people"] + stats["accounts"]
    assert stats["new_people_7d"] == stats["people"]  # accounts aren't counted under the People tile
    assert pages.kpi_tiles(1, stats)[0]["sub"] == f"{stats['people']} added in 7 days"


def test_source_filter_lists_each_source_once(client, company):
    assert len(ui.LEAD_SOURCES) == len(set(ui.LEAD_SOURCES))
    assert client.get(f"/c/{company.id}/leads").text.count('<option value="demo"') == 1
    assert client.get(f"/c/{company.id}/signals").text.count('<option value="demo"') == 1


def test_source_copy_uses_form_labels_and_points_reddit_at_the_server_setup(client, company):
    page = client.get(f"/c/{company.id}").text
    assert "Needs sec_queries" not in page and "Needs SEC EDGAR queries" in page
    assert 'Needs Reddit API app credentials' in page and 'href="/help#server-sources"' in page
    assert 'id="server-sources"' in client.get("/help").text
    repo.delete_company(company.id)
    welcome = client.get("/").text
    assert "out of the box: Hacker News, GitHub" in welcome and "Reddit works once the server has Reddit API" in welcome


def test_sent_messages_offer_open_not_edit(client, company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Omar Haddad", lead_company="Northwind"))
    draft = repo.create_message(lead.id, "Hi Omar")
    sent = repo.create_message(lead.id, "Hi again Omar", step=2)
    repo.update_message(sent.id, status="sent")
    base = f"/c/{company.id}"

    def actions(tab: str, msg_id: int) -> str:
        page = client.get(f"{base}/outreach?tab={tab}").text
        item = re.search(rf'<li class="card queue-item" id="q-{msg_id}">.*?</li>', page, re.S)
        return re.sub(r"<[^>]+>", " ", item.group(0))

    assert "Edit" in actions("drafts", draft.id)
    assert "Edit" not in actions("sent", sent.id) and "Open" in actions("sent", sent.id)


def test_csv_import_that_adds_nothing_is_a_warning_with_the_reason(client, company):
    base = f"/c/{company.id}"
    nameless = "Name,Email\n,a@example.com\n,b@example.com\n"
    resp = post(client, f"{base}/leads/import", files={"file": ("contacts.csv", nameless.encode(), "text/csv")})
    page = client.get(resp.headers["location"]).text
    assert "Imported contacts.csv" not in page
    assert re.search(r'flash-warning.*?Nothing was imported from contacts.csv: none of its 2 rows had a name or '
                     r'company', page, re.S)
    resp = post(client, f"{base}/leads/import", files={"file": ("empty.csv", b"Name,Company\n", "text/csv")})
    assert "Nothing was imported from empty.csv: it has no rows under the header line." in client.get(
        resp.headers["location"]).text


# --------------------------------------------------------------------------------------
# AI agent sending (the user's own browser agent sends approved LinkedIn messages)
# --------------------------------------------------------------------------------------

def approved_linkedin(company_id: int, name: str = "Omar Haddad", body: str = "Hi Omar, saw your post.",
                      channel: str = "linkedin_connect", slug: str = "omar-haddad"):
    lead, _ = repo.upsert_lead(company_id, LeadIn(full_name=name, title="Travel Manager", lead_company="Northwind",
                                                  linkedin_url=f"https://www.linkedin.com/in/{slug}"))
    return lead, repo.create_message(lead.id, body, channel=channel, status="approved")


def agent_on(company_id: int, limit: int = 15) -> None:
    repo.update_company(company_id, {"outreach": {"agent_sending": True, "agent_daily_limit": limit}})


def agent_card(page: str) -> str:
    match = re.search(r'<section class="card agent-card" id="agent".*?</section>', page, re.S)
    assert match, "no AI agent sending card"
    return match.group(0)


def text_of(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def test_agent_sending_is_off_by_default_and_toggles_with_csrf(client, company):
    base = f"/c/{company.id}"
    assert repo.get_company(company.id).outreach.agent_sending is False
    card = agent_card(client.get(f"{base}/outreach").text)
    assert "pill-agent-off" in card and "Turn on" in card and "Turn off" not in card
    assert "LinkedIn's rules forbid automation" in card
    assert "Nothing is queued while agent sending is off." in text_of(card)
    assert "agent-line" not in client.get(base).text

    # Without the dashboard's CSRF token nothing changes.
    assert client.post(f"{base}/outreach/agent", data={"agent_sending": "on"}).status_code == 403
    assert client.post(f"{base}/outreach/agent",
                       data={"agent_sending": "on", "csrf_token": "forged"}).status_code == 403
    assert repo.get_company(company.id).outreach.agent_sending is False

    resp = post(client, f"{base}/outreach/agent", {"agent_sending": "on", "agent_daily_limit": "8"})
    assert resp.status_code == 303 and resp.headers["location"] == f"{base}/outreach#agent"
    out = repo.get_company(company.id).outreach
    assert out.agent_sending is True and out.agent_daily_limit == 8
    page = client.get(f"{base}/outreach").text
    assert "AI agent sending is on: your agent may send up to 8 approved LinkedIn messages" in page
    card = agent_card(page)
    assert "pill-agent-on" in card and "On: 0 of 8 sent in the last 24 hours." in text_of(card)
    assert "Turn off" in card and "Save limit" in card
    dash = client.get(base).text
    assert "agent-line" in dash and "AI agent sending is on: 0 of 8 LinkedIn messages sent" in text_of(dash)

    # Saving a new limit keeps it on.
    post(client, f"{base}/outreach/agent", {"agent_sending": "on", "agent_daily_limit": "12"})
    out = repo.get_company(company.id).outreach
    assert out.agent_sending is True and out.agent_daily_limit == 12

    resp = post(client, f"{base}/outreach/agent", {"agent_sending": "off", "agent_daily_limit": "12"})
    assert resp.status_code == 303
    assert repo.get_company(company.id).outreach.agent_sending is False
    assert "AI agent sending is off" in client.get(f"{base}/outreach").text
    assert "agent-line" not in client.get(base).text


def test_agent_daily_limit_is_validated(client, company):
    base = f"/c/{company.id}"
    for bad in ("0", "51", "abc", "-3", "2.5", "100000"):
        resp = post(client, f"{base}/outreach/agent", {"agent_sending": "on", "agent_daily_limit": bad})
        assert resp.status_code == 303
        page = client.get(resp.headers["location"]).text
        assert "The daily limit must be a whole number from 1 to 50. Nothing was changed." in page, bad
        out = repo.get_company(company.id).outreach
        assert (out.agent_sending, out.agent_daily_limit) == (False, 15), bad
    assert forms.AGENT_LIMIT_RANGE == (1, 50)  # read from the model, which enforces it for every writer
    card = agent_card(client.get(f"{base}/outreach").text)
    assert 'name="agent_daily_limit" type="number" min="1" max="50"' in card

    # A blank limit keeps the stored one; the edges are allowed.
    post(client, f"{base}/outreach/agent", {"agent_sending": "on", "agent_daily_limit": ""})
    assert repo.get_company(company.id).outreach.agent_daily_limit == 15
    for edge in ("1", "50"):
        post(client, f"{base}/outreach/agent", {"agent_sending": "on", "agent_daily_limit": edge})
        assert repo.get_company(company.id).outreach.agent_daily_limit == int(edge)
    # Turning off always works, even with a bad limit in the field.
    post(client, f"{base}/outreach/agent", {"agent_sending": "off", "agent_daily_limit": "999"})
    out = repo.get_company(company.id).outreach
    assert (out.agent_sending, out.agent_daily_limit) == (False, 50)

    # The profile form validates the limit too.
    values = flat_values(repo.get_company(company.id))
    bad = post(client, f"{base}/settings",
               {**values, "outreach.agent_sending": "true", "outreach.agent_daily_limit": "60"})
    assert bad.status_code == 422 and 'id="f-outreach-agent_daily_limit-err"' in bad.text
    assert repo.get_company(company.id).outreach.agent_sending is False


def test_paused_agent_sending_shows_a_banner_and_can_be_resumed(client, company):
    base = f"/c/{company.id}"
    agent_on(company.id)
    lead, msg = approved_linkedin(company.id)
    reason = 'LinkedIn showed "You\'ve reached the weekly invitation limit" <script>alert(1)</script>'
    repo.report_send_problem(company.id, reason, message_id=msg.id)

    dash = client.get(base).text
    banner = re.search(r'<div class="flash flash-warning agent-banner".*?</div>', dash, re.S)
    assert banner, "no pause banner on the dashboard"
    assert "AI agent sending is paused" in banner.group(0) and f'href="{base}/outreach#agent"' in banner.group(0)
    assert "weekly invitation limit" in text_of(banner.group(0))
    assert "<script>alert(1)</script>" not in dash and "&lt;script&gt;alert(1)&lt;/script&gt;" in dash
    assert "agent-line" not in dash  # the "is on" line gives way to the warning

    page = client.get(f"{base}/outreach").text
    card = agent_card(page)
    assert "pill-agent-paused" in card and "Paused until" in text_of(card)
    assert f'action="{base}/outreach/agent/resume"' in card and "<script>alert(1)</script>" not in page
    assert "Nothing is queued while sending is paused." in text_of(card)
    assert f'id="agent-prompt-1"' in card  # the prompt is still there for after the pause
    # The message the agent was on stays approved, waiting for the user.
    assert repo.get_message(msg.id).status == "approved"

    # Saving the limit doesn't lift the pause; it says so.
    resp = post(client, f"{base}/outreach/agent", {"agent_sending": "on", "agent_daily_limit": "10"})
    assert "Saved, but agent sending is paused until" in client.get(resp.headers["location"]).text
    assert repo.get_company(company.id).outreach.agent_paused_until is not None

    assert client.post(f"{base}/outreach/agent/resume", data={}).status_code == 403
    assert repo.get_company(company.id).outreach.agent_paused_until is not None
    resp = post(client, f"{base}/outreach/agent/resume")
    assert resp.status_code == 303 and resp.headers["location"] == f"{base}/outreach#agent"
    out = repo.get_company(company.id).outreach
    assert out.agent_paused_until is None and out.agent_pause_reason == "" and out.agent_sending is True
    page = client.get(f"{base}/outreach").text
    assert "Agent sending resumed." in page and "pill-agent-on" in agent_card(page)
    dash = client.get(base).text
    assert "agent-banner" not in dash and "AI agent sending is on" in text_of(dash)


def test_a_pause_while_sending_is_off_is_shown_on_the_outreach_page_only(client, company):
    base = f"/c/{company.id}"
    repo.report_send_problem(company.id, "Security check page")
    assert "agent-banner" not in client.get(base).text
    card = agent_card(client.get(f"{base}/outreach").text)
    assert "pill-agent-off" in card and "Security check page" in card
    assert "Paused until" in text_of(card) and "after your agent reported a problem" in text_of(card)
    resp = post(client, f"{base}/outreach/agent/resume")
    assert "Pause lifted. Agent sending is still off" in client.get(resp.headers["location"]).text
    assert repo.get_company(company.id).outreach.agent_sending is False


def test_agent_queue_lists_approved_linkedin_messages_with_escaped_names(client, company):
    base = f"/c/{company.id}"
    evil = '<img src=x onerror=alert(1)>Mallory'
    lead, msg = approved_linkedin(company.id, name=evil, body="<b>Hello</b> there", slug="mallory")
    other, _ = repo.upsert_lead(company.id, LeadIn(full_name="Nadia Email", email="nadia@example.com",
                                                   linkedin_url="https://www.linkedin.com/in/nadia"))
    email = repo.create_message(other.id, "Email body", channel="email", subject="Hi", status="approved")
    draft = repo.create_message(other.id, "Just a draft", channel="linkedin_dm")
    no_url, _ = repo.upsert_lead(company.id, LeadIn(full_name="Karim NoProfile", lead_company="Fabrikam"))
    blocked = repo.create_message(no_url.id, "Hi Karim", channel="linkedin_dm", status="approved")

    # Off: nothing is queued, whatever is approved.
    assert "agent-queue" not in agent_card(client.get(f"{base}/outreach").text)

    agent_on(company.id)
    page = client.get(f"{base}/outreach").text
    assert "<img src=x" not in page and "<b>Hello</b>" not in page
    card = agent_card(page)
    queue = re.search(r'<ul class="list agent-queue">.*?</ul>', card, re.S).group(0)
    assert html_lib.escape(evil, quote=False) in queue and f"#msg-{msg.id}" in queue
    assert "LinkedIn connection note · step 1" in text_of(queue)
    assert f"#msg-{email.id}" not in queue and f"#msg-{draft.id}" not in queue and f"#msg-{blocked.id}" not in queue
    assert re.search(r'Queued for your agent <span class="count">1</span>', card)
    # Approved LinkedIn messages the agent may not send say why.
    skipped = re.search(r'<details class="agent-skipped">.*?</details>', card, re.S).group(0)
    assert "1 approved LinkedIn message can't be sent by your agent" in text_of(skipped)
    assert "Karim NoProfile" in skipped and "no LinkedIn profile URL" in skipped and f"#msg-{blocked.id}" in skipped
    # The approved tab marks what the agent will pick up.
    approved = client.get(f"{base}/outreach?tab=approved").text
    item = re.search(rf'<li class="card queue-item" id="q-{msg.id}">.*?</li>', approved, re.S).group(0)
    assert "Queued for your agent" in item
    item = re.search(rf'<li class="card queue-item" id="q-{email.id}">.*?</li>', approved, re.S).group(0)
    assert "Queued for your agent" not in item
    # Approving a LinkedIn draft says whether the agent will send it.
    resp = post(client, f"{base}/messages/{draft.id}", {"action": "approve", "next": f"{base}/outreach"})
    assert "Your AI agent will send it exactly as it is" in client.get(resp.headers["location"]).text
    later = repo.create_message(lead.id, "Follow-up", channel="linkedin_dm", step=2)
    resp = post(client, f"{base}/messages/{later.id}", {"action": "approve", "next": f"{base}/outreach"})
    assert ("It isn't in your AI agent's queue right now (another message to this lead is ahead in the queue"
            in html_lib.unescape(client.get(resp.headers["location"]).text))
    resp = post(client, f"{base}/messages/{email.id}", {"action": "approve", "next": f"{base}/outreach"})
    assert "Approved. Copy it and send it from LinkedIn or your inbox." in client.get(resp.headers["location"]).text


def test_agent_prompts_name_the_company_by_id_only(client, company):
    base = f"/c/{company.id}"
    agent_on(company.id)
    approved_linkedin(company.id, name="Omar Haddad", body="Secret approved text")
    card = agent_card(client.get(f"{base}/outreach").text)
    prompts = [html_lib.unescape(p) for p in re.findall(r'<p id="agent-prompt-\d+">(.*?)</p>', card, re.S)]
    assert prompts == [
        f"Use the openberry tools: run the send_approved_messages prompt for company {company.id}.",
        f"Use the openberry tools: call get_send_queue for company {company.id} and follow its instructions.",
    ]
    assert all("Omar" not in p and "Secret" not in p and "Northwind" not in p for p in prompts)
    assert [p for _, p in pages.agent_prompts(41)] == [p.replace(f"company {company.id}", "company 41")
                                                       for p in prompts]
    assert 'data-copy="#agent-prompt-1"' in card and 'data-copy="#agent-prompt-2"' in card
    assert f'href="{ui.AGENT_DOCS_URL}"' in card and ui.AGENT_DOCS_URL.endswith("/docs/AI_AGENT_SENDING.md")
    assert 'href="/help"' in card


def test_saving_the_profile_keeps_the_agent_pause(client, company):
    base = f"/c/{company.id}"
    agent_on(company.id, limit=9)
    paused = repo.report_send_problem(company.id, "Restricted account warning").outreach
    assert paused.agent_paused_until is not None

    page = client.get(f"{base}/settings").text
    assert re.search(r'name="outreach.agent_sending" value="true" checked', page)
    assert 'name="outreach.agent_daily_limit" type="number" value="9"' in page
    assert "LinkedIn's rules forbid automation" in page
    resp = browser_submit(client, f"{base}/settings", f"{base}/settings")
    assert resp.status_code == 303
    out = repo.get_company(company.id).outreach
    assert (out.agent_sending, out.agent_daily_limit) == (True, 9)
    assert out.agent_paused_until == paused.agent_paused_until
    assert out.agent_pause_reason == "Restricted account warning"

    # Unticking the box turns agent sending off; the pause stays until the user resumes it.
    values = {k: v for k, v in flat_values(repo.get_company(company.id)).items() if k != "outreach.agent_sending"}
    assert post(client, f"{base}/settings", values).status_code == 303
    out = repo.get_company(company.id).outreach
    assert out.agent_sending is False and out.agent_paused_until == paused.agent_paused_until
    assert _profile(company.id)["outreach"]["agent_pause_reason"] == "Restricted account warning"


def test_registration_sets_agent_sending_but_anonymous_visitors_cannot(client, web_settings):
    page = client.get("/register").text
    assert 'name="outreach.agent_sending"' in page and 'name="outreach.agent_daily_limit"' in page
    resp = post(client, "/register", {"name": "Agent Co", "outreach.agent_sending": "true",
                                      "outreach.agent_daily_limit": "5"}, page="/register")
    assert resp.status_code == 303
    created = next(c for c in repo.list_companies() if c.name == "Agent Co")
    assert (created.outreach.agent_sending, created.outreach.agent_daily_limit) == (True, 5)
    post(client, "/register", {"name": "Default Co"}, page="/register")
    default = next(c for c in repo.list_companies() if c.name == "Default Co")
    assert (default.outreach.agent_sending, default.outreach.agent_daily_limit) == (False, 15)

    web_settings.password = "pw"
    web_settings.public_registration = True
    page = client.get("/register").text
    assert "outreach.agent_sending" not in page
    resp = post(client, "/register", {"name": "Visitor Co", "outreach.agent_sending": "true"}, page="/register")
    assert resp.headers["location"] == "/register/thanks"
    visitor = next(c for c in repo.list_companies() if c.name == "Visitor Co")
    assert visitor.outreach.agent_sending is False and visitor.status == "paused"


def test_messages_the_agent_sent_are_labelled(client, company):
    base = f"/c/{company.id}"
    agent_on(company.id, limit=1)
    lead, msg = approved_linkedin(company.id)
    repo.confirm_agent_sent(msg.id)
    _, by_hand = approved_linkedin(company.id, name="Lina Hand", slug="lina-hand")
    repo.update_message(by_hand.id, status="sent")

    sent_tab = client.get(f"{base}/outreach?tab=sent").text
    item = re.search(rf'<li class="card queue-item" id="q-{msg.id}">.*?</li>', sent_tab, re.S).group(0)
    assert "Sent by AI agent" in item
    item = re.search(rf'<li class="card queue-item" id="q-{by_hand.id}">.*?</li>', sent_tab, re.S).group(0)
    assert "Sent by AI agent" not in item
    lead_page = client.get(f"{base}/leads/{lead.id}").text
    bubble = re.search(rf'<article class="bubble bubble-out" id="msg-{msg.id}">.*?</header>', lead_page, re.S)
    assert "Sent by AI agent" in bubble.group(0)
    assert ui.sent_via_label(types.SimpleNamespace(sent_via="claude")) == "Marked sent by Claude"
    assert ui.sent_via_label(types.SimpleNamespace()) == ""

    # The one-message limit is used up: the card and the dashboard say so.
    card = agent_card(client.get(f"{base}/outreach").text)
    assert "pill-agent-limit" in card and "1 of 1 sent in the last 24 hours" in text_of(card)
    assert "Your agent can send again from" in text_of(card)
    assert "daily limit reached" in text_of(client.get(base).text)


def test_the_json_api_cannot_turn_agent_sending_on_or_lift_its_pause(client, web_settings, company):
    web_settings.api_token = "tok"
    auth = {"Authorization": "Bearer tok"}
    url = f"/api/companies/{company.id}"
    for patch in ({"agent_sending": True}, {"agent_daily_limit": 16}, {"agent_pause_reason": "x"},
                  {"agent_paused_until": "2030-01-01T00:00:00+00:00"}):
        resp = client.patch(url, json={"outreach": patch}, headers=auth)
        assert resp.status_code == 403 and "only the user can" in resp.json()["detail"], patch
    assert client.patch(url, json={"outreach": {"agent_daily_limit": 99}}, headers=auth).status_code == 422
    out = repo.get_company(company.id).outreach
    assert (out.agent_sending, out.agent_daily_limit, out.agent_paused_until) == (False, 15, None)
    created = client.post("/api/companies", json={"name": "Script Co", "outreach": {"agent_sending": True}},
                          headers=auth)
    assert created.status_code == 403 and not any(c.name == "Script Co" for c in repo.list_companies())

    # Turning it off, lowering the limit and re-sending the stored profile unchanged are fine.
    agent_on(company.id, limit=10)
    paused = repo.report_send_problem(company.id, "Captcha page").outreach
    full = client.get(url, headers=auth).json()
    unchanged = client.patch(url, json={"outreach": full["outreach"], "requirements": "More"}, headers=auth)
    assert unchanged.status_code == 200
    assert client.patch(url, json={"outreach": {"agent_paused_until": None}}, headers=auth).status_code == 403
    resp = client.patch(url, json={"outreach": {"agent_sending": False, "agent_daily_limit": 5}}, headers=auth)
    assert resp.status_code == 200
    out = repo.get_company(company.id).outreach
    assert (out.agent_sending, out.agent_daily_limit) == (False, 5)
    assert out.agent_paused_until == paused.agent_paused_until and out.agent_pause_reason == "Captcha page"


def test_editing_an_approved_message_in_the_dashboard_needs_approval_again(client, company):
    base = f"/c/{company.id}"
    agent_on(company.id)
    lead, msg = approved_linkedin(company.id)
    page = client.get(f"{base}/leads/{lead.id}").text
    assert "Save &amp; approve" in page  # approved messages offer save + approve in one click
    lead_page = f"{base}/leads/{lead.id}"
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "save", "body": msg.body}, page=lead_page)
    assert resp.status_code == 303 and repo.get_message(msg.id).status == "approved"  # same text
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "save", "body": "Hi Omar, new words."}, page=lead_page)
    assert repo.get_message(msg.id).status == "draft" and repo.send_queue(company.id)["items"] == []
    assert "Approve it again" in client.get(resp.headers["location"]).text
    post(client, f"{base}/messages/{msg.id}", {"action": "approve", "body": "Hi Omar, newest words."}, page=lead_page)
    saved = repo.get_message(msg.id)
    assert (saved.status, saved.body) == ("approved", "Hi Omar, newest words.")
    assert [i["body"] for i in repo.send_queue(company.id)["items"]] == ["Hi Omar, newest words."]


def test_a_new_linkedin_profile_in_the_dashboard_needs_approval_again(client, company):
    base = f"/c/{company.id}"
    lead, msg = approved_linkedin(company.id)
    lead_page = f"{base}/leads/{lead.id}"
    post(client, f"{lead_page}/profile", {"full_name": lead.full_name, "linkedin_url": lead.linkedin_url,
                                          "title": "Head of Travel"}, page=lead_page)
    assert repo.get_message(msg.id).status == "approved"
    resp = post(client, f"{lead_page}/profile", {"full_name": lead.full_name,
                                                 "linkedin_url": "https://www.linkedin.com/in/other"}, page=lead_page)
    assert repo.get_message(msg.id).status == "draft"
    assert "drafts again" in client.get(resp.headers["location"]).text


def test_anonymous_registrations_get_the_default_agent_limit(client, web_settings):
    web_settings.password = "pw"
    web_settings.public_registration = True
    post(client, "/register", {"name": "Visitor Co", "outreach.agent_daily_limit": "50"}, page="/register")
    visitor = next(c for c in repo.list_companies() if c.name == "Visitor Co")
    assert (visitor.outreach.agent_sending, visitor.outreach.agent_daily_limit) == (False, 15)


# --------------------------------------------------------------------------------------
# The LinkedIn account: connection-note limits (free: 200 characters, 5 a month; Premium: 300) and the weekly cap
# --------------------------------------------------------------------------------------

def _set_account(company_id: int, account: str) -> None:
    repo.update_company(company_id, {"outreach": {"linkedin_account": account}})


def test_note_counters_follow_the_linkedin_account(client, company):
    base = f"/c/{company.id}"
    agent_on(company.id)
    lead, msg = approved_linkedin(company.id, body="Hi Omar, " + "x" * 241)  # 250 characters
    lead_page = f"{base}/leads/{lead.id}"

    page = client.get(lead_page).text
    assert f'id="msg-{msg.id}-body" name="body" rows="4" data-maxlen="200" data-counter="msg-{msg.id}-count"' in page
    counter = re.search(rf'<p class="counter[^"]*" id="msg-{msg.id}-count".*?</p>', page, re.S).group(0)
    assert 'class="counter over"' in counter and "250 / 200: too long for a connection note" in counter
    assert ("Notes from a free LinkedIn account: up to 200 characters, on 5 connection requests a month "
            "(Premium: 300 characters, every request)") in text_of(page)
    assert f'href="{base}/settings#f-outreach-linkedin_account"' in page
    approved_tab = client.get(f"{base}/outreach?tab=approved").text
    item = re.search(rf'<li class="card queue-item" id="q-{msg.id}">.*?</li>', approved_tab, re.S).group(0)
    assert ("250 / 200 characters: too long for a connection note from your free LinkedIn account"
            in text_of(item)) and "Queued for your agent" not in item
    # The agent won't send it, and approving it says why.
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "approve", "next": f"{base}/outreach"},
                page=lead_page)
    note = html_lib.unescape(client.get(resp.headers["location"]).text)
    assert ("It isn't in your AI agent's queue right now (the connection note is longer than 200 characters (250)"
            in note)

    _set_account(company.id, "premium")
    page = client.get(lead_page).text
    assert f'data-maxlen="300" data-counter="msg-{msg.id}-count"' in page
    counter = re.search(rf'<p class="counter[^"]*" id="msg-{msg.id}-count".*?</p>', page, re.S).group(0)
    assert 'class="counter"' in counter and "250 / 300" in counter and "too long" not in counter
    assert "Notes from a Premium LinkedIn account: up to 300 characters." in text_of(page)
    assert "5 connection requests a month" not in text_of(page)
    approved_tab = client.get(f"{base}/outreach?tab=approved").text
    item = re.search(rf'<li class="card queue-item" id="q-{msg.id}">.*?</li>', approved_tab, re.S).group(0)
    assert "250 / 300 characters" in text_of(item) and "too long" not in item and "Queued for your agent" in item


def test_template_drafts_in_the_dashboard_fit_the_linkedin_account(client, company):
    base = f"/c/{company.id}"
    repo.update_company(company.id, {
        "name": "Acme Executive Chauffeurs International", "value_proposition": "On-time chauffeurs",
        "icp": {"job_titles": ["Executive Assistant to the CEO", "Corporate Travel Manager"],
                "industries": ["Financial Services and Consulting"]},
        "outreach": {"sender_name": "Samantha Al-Rashid"}})
    lead, _ = repo.upsert_lead(company.id, LeadIn(
        full_name="Omar Haddad", lead_company="Northwind", linkedin_url="https://www.linkedin.com/in/omar-h",
        signals=[SignalIn(type="keyword_mention", source="reddit",
                          title="r/dubai: Need a reliable chauffeur for a three-day CEO roadshow across the UAE")]))
    lead_page = f"{base}/leads/{lead.id}"
    for account, low, high in (("free", 1, 200), ("premium", 201, 300)):
        _set_account(company.id, account)
        post(client, f"{lead_page}/draft", {"channel": "linkedin_connect", "step": "1", "engine": "template"},
             page=lead_page)
        note = repo.list_messages(company.id, lead_id=lead.id, limit=1)[0]
        assert note.channel == "linkedin_connect" and low <= len(note.body) <= high, (account, len(note.body))


def test_profile_form_sets_the_linkedin_account(client, company):
    base = f"/c/{company.id}"
    page = client.get(f"{base}/settings").text
    select = re.search(r'<select[^>]*name="outreach.linkedin_account".*?</select>', page, re.S).group(0)
    assert re.findall(r'<option value="(\w+)"( selected)?>([^<]+)</option>', select) == [
        ("free", " selected", "Free (Basic)"), ("premium", "", "Premium")]
    assert ("Sets LinkedIn's connection-note limits: free accounts can add a note to 5 requests a month, "
            "200 characters each; Premium to every request, 300 characters.") in html_lib.unescape(page)
    # Step 5, Outreach & alerts (after its channels, before the agent settings).
    assert (page.index('id="step-outreach"') < page.index('name="outreach.channels"')
            < page.index('name="outreach.linkedin_account"') < page.index('name="outreach.agent_sending"'))

    values = {**flat_values(repo.get_company(company.id)), "outreach.linkedin_account": "premium"}
    assert post(client, f"{base}/settings", values).status_code == 303
    assert repo.get_company(company.id).outreach.linkedin_account == "premium"
    assert '<option value="premium" selected>Premium</option>' in client.get(f"{base}/settings").text
    assert browser_submit(client, f"{base}/settings", f"{base}/settings").status_code == 303
    assert repo.get_company(company.id).outreach.linkedin_account == "premium"  # an unrelated save keeps it
    bad = post(client, f"{base}/settings", {**values, "outreach.linkedin_account": "business"})
    assert bad.status_code == 422 and 'id="f-outreach-linkedin_account-err"' in bad.text
    assert repo.get_company(company.id).outreach.linkedin_account == "premium"

    register = client.get("/register").text
    assert 'name="outreach.linkedin_account"' in register and "Free (Basic)" in register
    post(client, "/register", {"name": "Premium Co", "outreach.linkedin_account": "premium"}, page="/register")
    post(client, "/register", {"name": "Basic Co"}, page="/register")
    accounts = {c.name: c.outreach.linkedin_account for c in repo.list_companies()}
    assert (accounts["Premium Co"], accounts["Basic Co"]) == ("premium", "free")


def test_outreach_card_shows_the_connection_limits(client, company):
    base = f"/c/{company.id}"
    card = text_of(agent_card(client.get(f"{base}/outreach").text))
    assert "Connection requests this week: 0 of 80" in card
    assert "Notes this month: 0 of 5 (free LinkedIn account, notes up to 200 characters)" in card

    earlier, _ = repo.upsert_lead(company.id, LeadIn(full_name="Earlier Lead",
                                                     linkedin_url="https://www.linkedin.com/in/earlier"))
    for _ in range(2):  # sent by hand: LinkedIn counts them, so OpenBerry does too
        repo.create_message(earlier.id, "Hi, happy to connect!", channel="linkedin_connect", status="sent")
    card = text_of(agent_card(client.get(f"{base}/outreach").text))
    assert "Connection requests this week: 2 of 80" in card and "Notes this month: 2 of 5" in card
    assert "on hold" not in card

    agent_on(company.id)
    for _ in range(3):
        repo.create_message(earlier.id, "Hi, happy to connect!", channel="linkedin_connect", status="sent")
    _, connect = approved_linkedin(company.id, name="Nadia Connect", slug="nadia-connect")
    _, dm = approved_linkedin(company.id, name="Karim Message", body="Thanks for connecting!",
                              channel="linkedin_dm", slug="karim-message")
    page = agent_card(client.get(f"{base}/outreach").text)
    card = text_of(page)
    assert "Notes this month: 5 of 5" in card and "Connection requests are on hold." in card
    assert ("Free LinkedIn accounts can add a note to only 5 connection requests a month, and 5 were sent in the "
            "last 30 days.") in card and "LinkedIn messages still go out." in card
    assert "Your agent can send connection requests again from" in card
    assert f'href="{base}/settings#f-outreach-linkedin_account"' in page
    queue = re.search(r'<ul class="list agent-queue">.*?</ul>', page, re.S).group(0)
    assert f"#msg-{dm.id}" in queue and f"#msg-{connect.id}" not in queue  # the DM still goes
    skipped = re.search(r'<details class="agent-skipped">.*?</details>', page, re.S).group(0)
    assert "Nadia Connect" in skipped and "add a note to only 5 connection requests a month" in text_of(skipped)

    _set_account(company.id, "premium")
    for _ in range(75):
        repo.create_message(earlier.id, "Hi, happy to connect!", channel="linkedin_connect", status="sent")
    card = text_of(agent_card(client.get(f"{base}/outreach").text))
    assert "Connection requests this week: 80 of 80" in card and "Notes this month" not in card
    assert "Premium LinkedIn account: a note on every request, up to 300 characters" in card
    assert ("80 connection requests were sent in the last 7 days. Your agent sends at most 80 a week, to stay below "
            "LinkedIn's weekly invitation limit") in card and "LinkedIn messages still go out." in card


def test_a_note_edited_in_the_dashboard_counts_each_line_break_once(client, company):
    """Browsers submit a textarea's line breaks as CR LF; the counter (and LinkedIn) count one character each."""
    base = f"/c/{company.id}"
    agent_on(company.id)
    lead, msg = approved_linkedin(company.id)
    lines = ["Hi Omar,", "x" * 186, "Sam"]  # 8 + 186 + 3 characters and 2 line breaks: 199, under the free 200
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "approve", "body": "\r\n".join(lines)},
                page=f"{base}/leads/{lead.id}")
    assert resp.status_code == 303
    saved = repo.get_message(msg.id)
    assert saved.body == "\n".join(lines) and len(saved.body) == 199 and saved.status == "approved"
    assert [i["message_id"] for i in repo.send_queue(company.id)["items"]] == [msg.id]
    page = client.get(f"{base}/leads/{lead.id}").text
    assert re.search(rf'id="msg-{msg.id}-count"[^>]*>199 / 200</p>', page)
    # Editing the same text again doesn't count as a change: it stays approved.
    post(client, f"{base}/messages/{msg.id}", {"action": "save", "body": "\r\n".join(lines)},
         page=f"{base}/leads/{lead.id}")
    assert repo.get_message(msg.id).status == "approved"


def test_a_sent_note_is_never_flagged_too_long(client, company):
    """A note sent from Premium (or by hand) before the account was set to free went out: nothing to shorten."""
    base = f"/c/{company.id}"
    _set_account(company.id, "premium")
    _, msg = approved_linkedin(company.id, body="Hi Omar, " + "x" * 241)  # 250 characters
    repo.update_message(msg.id, status="sent")
    _set_account(company.id, "free")
    sent_tab = client.get(f"{base}/outreach?tab=sent").text
    raw = re.search(rf'<li class="card queue-item" id="q-{msg.id}">.*?</li>', sent_tab, re.S).group(0)
    item = text_of(raw)
    assert "250 characters" in item and "/ 200" not in item and "too long" not in item and "shorten" not in item
    assert "counter over" not in raw


def test_flash_messages_float_where_they_are_seen():
    """Redirects land on a section (#agent, #outreach): the confirmation must not be scrolled out of view."""
    static = Path(__file__).parent.parent / "src" / "openberry" / "web" / "static"
    css = (static / "app.css").read_text(encoding="utf-8")
    flashes_rule = re.search(r"\.flashes \{[^}]*\}", css).group(0)
    assert "position: fixed" in flashes_rule
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "function initFlashes()" in js and "initFlashes();" in js
    assert ".flashes .flash-success, .flashes .flash-info" in js  # errors and warnings are never auto-dismissed


# --------------------------------------------------------------------------------------
# Bulk approve / skip on the Outreach page's Drafts tab
# --------------------------------------------------------------------------------------

class _BulkPage(HTMLParser):
    """The bulk form's checkboxes (inputs with form="bulk-form") and any form opened inside another one."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth, self.nested, self.bulk_form = 0, False, False
        self.boxes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.nested |= self.depth > 0
            self.depth += 1
            self.bulk_form |= a.get("id") == "bulk-form"
        elif tag == "input" and a.get("form") == "bulk-form" and a.get("type") == "checkbox":
            self.boxes.append(a.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.depth -= 1


def bulk_page(client: TestClient, path: str) -> _BulkPage:
    reader = _BulkPage()
    reader.feed(client.get(path).text)
    return reader


def draft_for(company_id: int, name: str, body: str = "", channel: str = "linkedin_dm", subject: str = ""):
    slug = name.lower().replace(" ", "-")
    lead, _ = repo.upsert_lead(company_id, LeadIn(full_name=name, lead_company="Northwind",
                                                  linkedin_url=f"https://www.linkedin.com/in/{slug}"))
    return repo.create_message(lead.id, body or f"Hi {name.split()[0]}, saw your post.", channel=channel,
                               subject=subject)


def bulk(client: TestClient, company_id: int, action: str, values: list[str]):
    base = f"/c/{company_id}"
    return client.post(f"{base}/outreach/bulk", follow_redirects=False, data={
        "csrf_token": token(client, f"{base}/outreach"), "action": action, "message": values,
        "next": f"{base}/outreach?tab=drafts"})


def picked(*messages) -> list[str]:
    return [f"{msg.id}:{repo.message_version(msg)}" for msg in messages]


def test_bulk_approve_and_skip_ticked_drafts(client, company):
    base = f"/c/{company.id}"
    note = draft_for(company.id, "Omar Haddad", channel="linkedin_connect")
    dm = draft_for(company.id, "Sara Ali")
    mail = draft_for(company.id, "Lina Noor", channel="email", subject="Airport transfers")

    page = bulk_page(client, f"{base}/outreach")
    assert page.bulk_form and not page.nested  # the checkboxes join the form by its id: no nested forms
    assert sorted(page.boxes) == sorted(picked(note, dm, mail))
    html = client.get(f"{base}/outreach").text
    assert "Approve selected" in html and "Skip selected" in html and "Select the draft to Omar Haddad" in html

    resp = bulk(client, company.id, "approve", picked(note, dm))
    assert resp.status_code == 303 and resp.headers["location"] == f"{base}/outreach?tab=drafts"
    assert [repo.get_message(m.id).status for m in (note, dm, mail)] == ["approved", "approved", "draft"]
    flashed = text_of(client.get(resp.headers["location"]).text)
    assert "Approved 2 drafts. Send them from the Approved tab." in flashed
    assert bulk_page(client, f"{base}/outreach").boxes == picked(mail)

    resp = bulk(client, company.id, "skip", picked(mail))
    assert repo.get_message(mail.id).status == "skipped"
    assert "Skipped 1 draft." in text_of(client.get(resp.headers["location"]).text)
    # Only the Drafts tab offers it.
    for tab in ("approved", "sent", "replies", "followups"):
        assert bulk_page(client, f"{base}/outreach?tab={tab}").boxes == []
        assert 'id="bulk-form"' not in client.get(f"{base}/outreach?tab={tab}").text


def test_bulk_approve_with_agent_sending_on_says_the_agent_may_send(client, company):
    agent_on(company.id)
    dm = draft_for(company.id, "Sara Ali")
    resp = bulk(client, company.id, "approve", picked(dm))
    assert "Approved 1 draft. Your AI agent can send the LinkedIn ones." in text_of(
        client.get(resp.headers["location"]).text)
    assert [i["message_id"] for i in repo.send_queue(company.id)["items"]] == [dm.id]


def test_bulk_approve_only_takes_this_companys_drafts_as_shown(client, company):
    other = repo.create_company(CompanyIn(name="Other Co"))
    theirs = draft_for(other.id, "Someone Else")
    shown = draft_for(company.id, "Sara Ali")
    approved = draft_for(company.id, "Omar Haddad")
    repo.update_message(approved.id, status="approved")
    long_note = draft_for(company.id, "Lina Noor", body="Hi Lina, " + "x" * 241, channel="linkedin_connect")
    good = draft_for(company.id, "Ali Reza")
    stale = picked(shown)
    repo.update_message(shown.id, body="Hi Sara, Claude rewrote this after the page was opened.")

    resp = bulk(client, company.id, "approve", [*picked(theirs, approved, long_note, good), *stale])
    assert repo.get_message(theirs.id).status == "draft"  # another company's draft, even with its fingerprint
    assert repo.get_message(shown.id).status == "draft"  # the text changed after the page was shown
    assert repo.get_message(long_note.id).status == "draft"  # 250 characters: too long for a free account
    assert repo.get_message(approved.id).status == "approved" and repo.get_message(good.id).status == "approved"
    flashed = text_of(client.get(resp.headers["location"]).text)
    assert ("Approved 1 draft. Send it from the Approved tab. 4 not approved: 1 not found, 1 no longer a draft, "
            "1 connection note too long and 1 edited since the page opened. Review the edited ones, then try again."
            ) in flashed

    assert "…" not in flashed[flashed.index("Approved 1 draft"):flashed.index("try again.")]
    # Skipping doesn't care about the note's length, but still needs the text that was shown.
    bulk(client, company.id, "skip", [*picked(long_note), *stale])
    assert repo.get_message(long_note.id).status == "skipped" and repo.get_message(shown.id).status == "draft"


def test_bulk_actions_refuse_bad_requests(client, company):
    base = f"/c/{company.id}"
    dm = draft_for(company.id, "Sara Ali")
    assert client.post(f"{base}/outreach/bulk", data={"action": "approve", "message": picked(dm)}).status_code == 403
    for action, values, expected in (
        ("approve", [], "Tick the drafts you want first"),
        ("approve", ["abc", f"{dm.id}", f"-{dm.id}:x", ":"], "Tick the drafts you want first"),
        ("send", picked(dm), "Unknown action."),
        ("approve", [f"{i}:abc" for i in range(1, repo.BULK_MESSAGES_MAX + 2)], "Select at most 200 drafts"),
        ("approve", [f"{dm.id}:0000000000000000"], "Nothing was approved: 1 edited since the page opened. Review the edited ones"),
    ):
        resp = bulk(client, company.id, action, values)
        assert resp.status_code == 303 and expected in text_of(client.get(resp.headers["location"]).text)
        assert repo.get_message(dm.id).status == "draft"
    # The same draft ticked twice is approved once.
    resp = bulk(client, company.id, "approve", picked(dm) * 2)
    assert "Approved 1 draft." in text_of(client.get(resp.headers["location"]).text)
    assert bulk(client, 9999, "approve", picked(dm)).status_code == 404
    with pytest.raises(ValueError):
        repo.bulk_update_drafts(company.id, picked_pairs(dm), "send")


def picked_pairs(*messages) -> list[tuple[int, str]]:
    return [(msg.id, repo.message_version(msg)) for msg in messages]


def test_bulk_bar_script_keeps_the_clicked_action():
    js = (Path(__file__).parent.parent / "src" / "openberry" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function initBulk()" in js and "initBulk();" in js
    # A button disabled before the browser reads the form would drop action=approve from the request.
    assert "setTimeout(() => buttons.forEach((btn) => { btn.disabled = true; }), 0)" in js


def test_outreach_tabs_and_list_actions_keep_your_place(client, company):
    """Tabs and list actions reload the page: they must not throw the user back to the top (app.js initKeepScroll)."""
    base = f"/c/{company.id}"
    draft_for(company.id, "Sara Ali")
    approved = draft_for(company.id, "Omar Haddad")
    repo.update_message(approved.id, status="approved")
    for tab in ("drafts", "approved", "sent", "replies", "followups"):
        page = client.get(f"{base}/outreach?tab={tab}").text
        assert re.search(r'<nav class="tabs" aria-label="Outreach queue" data-scroll-anchor>', page)
        assert page.count("data-scroll-anchor") == 1  # one anchor per page: the tabs
    drafts = client.get(f"{base}/outreach?tab=drafts").text
    # Approve, Mark sent and the bulk bar come back to this page: each remembers the position.
    forms = re.findall(r'<form method="post" action="[^"]*/(?:messages/\d+|outreach/bulk)"[^>]*>', drafts)
    assert len(forms) == 3 and all("data-keep-scroll" in f for f in forms)
    approved_tab = client.get(f"{base}/outreach?tab=approved").text
    forms = re.findall(r'<form method="post" action="[^"]*/messages/\d+"[^>]*>', approved_tab)
    assert len(forms) == 1 and "data-keep-scroll" in forms[0]  # Mark sent
    # The agent card's forms land on #agent instead, so they don't keep the position.
    agent = re.findall(r'<form method="post" action="[^"]*/outreach/agent[^"]*"[^>]*>', drafts)
    assert agent and not any("data-keep-scroll" in f for f in agent)


def test_keep_scroll_script():
    js = (Path(__file__).parent.parent / "src" / "openberry" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function initKeepScroll()" in js
    init = js[js.index("function init() {"):]
    assert init.index("initKeepScroll();") < init.index("initNav();")  # restore before anything else runs
    keep = js[js.index("function rememberScroll"):js.index("function initKeepScroll")]
    assert "state.path !== location.pathname || location.hash" in keep  # other pages and #section links: as usual
    assert "sessionStorage" in keep and "try {" in keep  # storage can be blocked


# --------------------------------------------------------------------------------------
# Auto-approve: the Outreach page's card, a line under each draft, Hold / Let it auto-approve, the pill
# --------------------------------------------------------------------------------------

def auto_card(page: str) -> str:
    match = re.search(r'<section class="card agent-card auto-card" id="auto-approve".*?</section>', page, re.S)
    assert match, "no auto-approve card"
    return match.group(0)


def auto_on(company_id: int, hours: int = 2) -> None:
    """Auto-approve on since long ago, so each draft's own time decides when it is approved."""
    since = repo.iso(repo.utcnow() - timedelta(days=30))
    repo.update_company(company_id, {"outreach": {"auto_approve": True, "auto_approve_hours": hours,
                                                  "auto_approve_since": since}})


def aged(message_id: int, hours: float) -> None:
    """As if the message was written `hours` ago."""
    from openberry import db

    stamp = repo.iso(repo.utcnow() - timedelta(hours=hours))
    with db.connect() as c:
        c.execute("UPDATE messages SET created_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, message_id))


def queue_item(page: str, message_id: int) -> str:
    match = re.search(rf'<li class="card queue-item" id="q-{message_id}">.*?</li>', page, re.S)
    assert match, f"message {message_id} is not listed"
    return match.group(0)


def flashed(client: TestClient, resp) -> str:
    return html_lib.unescape(client.get(resp.headers["location"]).text)


def test_auto_approve_is_off_by_default_and_toggles_with_csrf(client, company):
    base = f"/c/{company.id}"
    page = client.get(f"{base}/outreach").text
    card = auto_card(page)
    assert "pill-agent-off" in card and "Off. Drafts wait for you to approve them." in text_of(card)
    assert "Turn on" in card and "Turn off" not in card and "Never approved automatically: drafts you hold" in card
    confirm = html_lib.unescape(re.search(r'data-confirm="([^"]*)"', card).group(1))
    assert "your agent may then send them on LinkedIn without you reading them" in confirm
    assert page.index('id="agent"') < page.index('id="auto-approve"') < page.index('class="tabs"')

    # Without the dashboard's CSRF token nothing changes.
    assert client.post(f"{base}/outreach/auto-approve", data={"auto_approve": "on"}).status_code == 403
    assert client.post(f"{base}/outreach/auto-approve",
                       data={"auto_approve": "on", "csrf_token": "forged"}).status_code == 403
    assert repo.get_company(company.id).outreach.auto_approve is False

    backlog = draft_for(company.id, "Sara Ali")
    aged(backlog.id, 24 * 7)
    before = repo.utcnow()
    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "3"})
    assert resp.status_code == 303 and resp.headers["location"] == f"{base}/outreach#auto-approve"
    out = repo.get_company(company.id).outreach
    assert (out.auto_approve, out.auto_approve_hours) == (True, 3) and out.auto_approve_since >= before
    page = flashed(client, resp)
    assert ("Auto-approve is on: drafts you don't edit, hold or skip are approved 3 hours after they're written. "
            "Drafts you already have get 3 hours from now.") in page
    assert repo.get_message(backlog.id).status == "draft"  # a week old, but it gets the full window from now
    card = text_of(auto_card(page))
    assert "pill-agent-on" in auto_card(page) and "Save" in card and "Turn off" in card
    assert "On: drafts you don't edit, hold or skip are approved 3 hours after they're written." in card
    next_at = out.auto_approve_since + timedelta(hours=3)
    assert f"1 draft waiting; the next one is approved at {ui.abs_dt(next_at)}" in card

    # Save keeps it on, and keeps when it was turned on.
    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "5"})
    saved = repo.get_company(company.id).outreach
    assert (saved.auto_approve, saved.auto_approve_hours, saved.auto_approve_since) == (True, 5,
                                                                                      out.auto_approve_since)
    assert "Review window saved: drafts are approved 5 hours after they're written or last edited." in flashed(
        client, resp)

    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "off", "auto_approve_hours": "5"})
    assert repo.get_company(company.id).outreach.auto_approve is False
    page = flashed(client, resp)
    assert "Auto-approve is off. Drafts wait for you to approve them." in page and "pill-agent-off" in auto_card(page)

    # On a paused company it says nothing is approved until it is active again.
    repo.update_company(company.id, {"status": "paused"})
    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "2"})
    page = flashed(client, resp)
    assert "The company is paused: nothing is approved until you activate it." in page
    assert "The company is paused, so nothing is approved until you activate it." in text_of(auto_card(page))
    # With AI agent sending on, turning it on says the agent may send what it approves.
    repo.update_company(company.id, {"status": "active", "outreach": {"agent_sending": True, "auto_approve": False}})
    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "2"})
    page = flashed(client, resp)
    assert "With AI agent sending on, your agent may then send the LinkedIn ones." in page
    assert "Auto-approve is on: drafts you don't edit, hold or skip in time are approved, so your agent may send" in (
        text_of(agent_card(page)))


def test_the_auto_approve_window_is_validated(client, company):
    base = f"/c/{company.id}"
    for bad in ("0", "73", "abc", "-3", "2.5", "1000"):
        resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": bad})
        assert resp.status_code == 303
        assert ("The review window must be a whole number of hours from 1 to 72. Nothing was changed."
                in flashed(client, resp)), bad
        out = repo.get_company(company.id).outreach
        assert (out.auto_approve, out.auto_approve_hours, out.auto_approve_since) == (False, 2, None), bad
    assert forms.AUTO_APPROVE_HOURS_RANGE == (1, 72)  # read from the model, which enforces it for every writer
    assert 'name="auto_approve_hours" type="number" min="1" max="72"' in auto_card(client.get(f"{base}/outreach").text)

    # A blank window keeps the stored one; the edges are allowed.
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": ""})
    assert repo.get_company(company.id).outreach.auto_approve_hours == 2
    for edge in ("1", "72"):
        post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": edge})
        assert repo.get_company(company.id).outreach.auto_approve_hours == int(edge)
    # A bad value while it is on changes nothing either; turning off always works.
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "0"})
    out = repo.get_company(company.id).outreach
    assert (out.auto_approve, out.auto_approve_hours) == (True, 72)
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "off", "auto_approve_hours": "999"})
    out = repo.get_company(company.id).outreach
    assert (out.auto_approve, out.auto_approve_hours) == (False, 72)


def test_drafts_say_when_they_are_approved_automatically(client, company):
    base = f"/c/{company.id}"
    waiting = draft_for(company.id, "Sara Ali")
    held = draft_for(company.id, "Omar Haddad")
    repo.set_auto_hold(held.id, True)
    repo.update_company(company.id, {"outreach": {"banned_words": ["synergy"]}})
    blocked = draft_for(company.id, "Lina Noor", body="Real synergy here, Lina.")
    assert "auto-line" not in client.get(f"{base}/outreach").text  # off: no lines, no buttons

    auto_on(company.id)
    aged(waiting.id, 1)
    written = repo.get_message(waiting.id).updated_at
    page = client.get(f"{base}/outreach").text
    assert not bulk_page(client, f"{base}/outreach").nested  # the Hold forms aren't inside another form
    item = queue_item(page, waiting.id)
    at = repo.get_message(waiting.id).updated_at + timedelta(hours=2)
    assert f"Approves automatically at {ui.abs_dt(at)} (in " in text_of(item)
    assert f'<time datetime="{ui.iso_dt(at)}">' in item
    line = re.search(r'<div class="auto-line auto-waiting">.*?</div>', item, re.S).group(0)
    assert f'action="{base}/messages/{waiting.id}" data-keep-scroll' in line
    assert f'name="next" value="{base}/outreach?tab=drafts"' in line and 'value="hold"' in line
    item = queue_item(page, held.id)
    assert "On hold: approve it yourself" in text_of(item) and 'value="release"' in item
    assert "Let it auto-approve" in item
    item = queue_item(page, blocked.id)
    assert "Not approved automatically for now: it uses a banned word or phrase (synergy)" in text_of(item)
    assert 'value="hold"' in item and 'value="release"' not in item  # it can be held before the reason goes away

    # Hold: back on the same tab.
    resp = post(client, f"{base}/messages/{waiting.id}", {"action": "hold", "next": f"{base}/outreach?tab=drafts"},
                page=f"{base}/outreach")
    assert resp.headers["location"] == f"{base}/outreach?tab=drafts" and repo.get_message(waiting.id).auto_hold
    assert "On hold: this draft won't be approved automatically." in flashed(client, resp)
    # Let it auto-approve: the hold is lifted and the draft waits a new window from now.
    resp = post(client, f"{base}/messages/{waiting.id}",
                {"action": "release", "next": f"{base}/outreach?tab=drafts"}, page=f"{base}/outreach")
    released = repo.get_message(waiting.id)
    assert released.auto_hold is False and released.updated_at > written
    assert ("Hold lifted: this draft is approved automatically in 2 hours unless you edit, hold or skip it."
            in flashed(client, resp))
    # A draft that can't be approved yet: holding it works, and lifting the hold says why it still waits.
    post(client, f"{base}/messages/{blocked.id}", {"action": "hold"}, page=f"{base}/outreach")
    assert repo.get_message(blocked.id).auto_hold
    resp = post(client, f"{base}/messages/{blocked.id}", {"action": "release"}, page=f"{base}/outreach")
    assert ("Hold lifted, but it isn't approved automatically for now: it uses a banned word or phrase (synergy)."
            in flashed(client, resp))
    # Only drafts are held; the CSRF token is required.
    repo.update_message(blocked.id, status="approved")
    resp = post(client, f"{base}/messages/{blocked.id}", {"action": "hold"}, page=f"{base}/outreach")
    assert "Only drafts can be put on hold" in flashed(client, resp) and not repo.get_message(blocked.id).auto_hold
    assert client.post(f"{base}/messages/{held.id}", data={"action": "release"}).status_code == 403
    assert repo.get_message(held.id).auto_hold


def test_the_lead_page_shows_each_drafts_line_and_back_to_drafts_holds(client, company):
    base = f"/c/{company.id}"
    auto_on(company.id)
    msg = draft_for(company.id, "Sara Ali")
    lead_url = f"{base}/leads/{msg.lead_id}"
    page = client.get(lead_url).text
    assert not bulk_page(client, lead_url).nested
    assert "Drafts you don't edit, hold or skip are approved automatically 2 hours after they're written" in (
        html_lib.unescape(page)) and f'href="{base}/outreach#auto-approve"' in page
    bubble = re.search(rf'<article class="bubble bubble-out bubble-edit" id="msg-{msg.id}">.*?</article>', page,
                       re.S).group(0)
    line = re.search(r'<div class="auto-line auto-waiting">.*?</div>', bubble, re.S).group(0)
    assert "Approves automatically at" in text_of(line) and "data-keep-scroll" not in line
    # Hold belongs to the draft's edit form, so what the user typed there is saved with it, not lost.
    assert "<form" not in line and f'value="hold" form="msg-{msg.id}-form"' in line
    assert f'<form method="post" action="{base}/messages/{msg.id}" id="msg-{msg.id}-form">' in bubble
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "hold", "body": "Hi Sara, typed but not saved yet.",
                                                     "next": f"{lead_url}#msg-{msg.id}"}, page=lead_url)
    assert resp.headers["location"] == f"{lead_url}#msg-{msg.id}"
    held = repo.get_message(msg.id)
    assert (held.status, held.auto_hold, held.body) == ("draft", True, "Hi Sara, typed but not saved yet.")
    assert "On hold: approve it yourself" in text_of(client.get(lead_url).text)
    # Unchanged text isn't saved again; an emptied text box is refused, as with Save, and nothing is held.
    post(client, f"{base}/messages/{msg.id}", {"action": "release", "body": held.body}, page=lead_url)
    released = repo.get_message(msg.id)
    post(client, f"{base}/messages/{msg.id}", {"action": "hold", "body": held.body}, page=lead_url)
    assert repo.get_message(msg.id).updated_at == released.updated_at and repo.get_message(msg.id).auto_hold
    post(client, f"{base}/messages/{msg.id}", {"action": "release", "body": held.body}, page=lead_url)
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "hold", "body": "  "}, page=lead_url)
    assert "message body is empty" in flashed(client, resp) and not repo.get_message(msg.id).auto_hold
    msg = repo.get_message(msg.id)

    # An approved message can go back to drafts, held: auto-approve won't approve it behind the user's back.
    repo.update_message(msg.id, status="approved")
    page = client.get(lead_url).text
    bubble = re.search(rf'<article class="bubble bubble-out bubble-edit" id="msg-{msg.id}">.*?</article>', page,
                       re.S).group(0)
    assert 'value="draft"' in bubble and "Back to drafts" in bubble and "auto-line" not in bubble
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "draft", "body": msg.body,
                                                     "next": f"{lead_url}#msg-{msg.id}"}, page=lead_url)
    back = repo.get_message(msg.id)
    assert (back.status, back.auto_hold) == ("draft", True)
    assert "Moved back to drafts and put on hold: it won't be approved automatically." in flashed(client, resp)


def test_opening_the_outreach_or_a_lead_page_approves_due_drafts(client, web_settings, company, monkeypatch):
    assert web_settings.scheduler_enabled is False  # no scheduler: the pages themselves keep it current
    auto_on(company.id)
    first = draft_for(company.id, "Sara Ali")
    aged(first.id, 3)
    page = client.get(f"/c/{company.id}/outreach").text
    assert repo.get_message(first.id).status == "approved" and f'id="q-{first.id}"' not in page
    second = draft_for(company.id, "Omar Haddad")
    aged(second.id, 3)
    client.get(f"/c/{company.id}/leads/{second.lead_id}")
    assert repo.get_message(second.id).status == "approved"
    # A page view while nothing is due takes no write lock (a scan can hold it for a while).
    third = draft_for(company.id, "Lina Noor")
    calls, real = [], repo._write_locked

    def spy(conn):
        calls.append(conn)
        return real(conn)

    monkeypatch.setattr(repo, "_write_locked", spy)
    client.get(f"/c/{company.id}/outreach")
    assert calls == [] and repo.get_message(third.id).status == "draft"


def test_auto_approved_messages_carry_a_pill(client, company):
    base = f"/c/{company.id}"
    auto_on(company.id)
    auto = draft_for(company.id, "Sara Ali")
    aged(auto.id, 3)
    repo.auto_approve_due(company.id)
    by_hand = draft_for(company.id, "Omar Haddad")
    repo.update_message(by_hand.id, status="approved")

    approved = client.get(f"{base}/outreach?tab=approved").text
    assert "Auto-approved" in queue_item(approved, auto.id)
    assert "Auto-approved" not in queue_item(approved, by_hand.id)
    lead_page = client.get(f"{base}/leads/{auto.lead_id}").text
    head = re.search(rf'<article class="bubble bubble-out bubble-edit" id="msg-{auto.id}">.*?</header>', lead_page,
                     re.S).group(0)
    assert "pill-auto-approved" in head and "Auto-approved" in head
    # Still shown once it was sent: it went out without a person approving it.
    repo.update_message(auto.id, status="sent")
    assert "Auto-approved" in queue_item(client.get(f"{base}/outreach?tab=sent").text, auto.id)
    head = re.search(rf'<article class="bubble bubble-out" id="msg-{auto.id}">.*?</header>',
                     client.get(f"{base}/leads/{auto.lead_id}").text, re.S).group(0)
    assert "Auto-approved" in head


def test_saving_the_profile_keeps_the_auto_approve_settings(client, company):
    base = f"/c/{company.id}"
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "6"})
    stored = repo.get_company(company.id).outreach
    page = client.get(f"{base}/settings").text
    assert 'name="outreach.auto_approve' not in page  # set on the Outreach page only
    assert browser_submit(client, f"{base}/settings", f"{base}/settings").status_code == 303
    out = repo.get_company(company.id).outreach
    assert (out.auto_approve, out.auto_approve_hours, out.auto_approve_since) == (
        True, 6, stored.auto_approve_since)
    # Fields posted by hand are ignored too.
    values = flat_values(repo.get_company(company.id))
    post(client, f"{base}/settings", {**values, "outreach.auto_approve": "", "outreach.auto_approve_hours": "1"})
    assert repo.get_company(company.id).outreach.auto_approve_hours == 6

    # A whole profile read before the user turned it off can't turn it back on (nor the reverse).
    stale = CompanyIn.model_validate(repo.get_company(company.id).model_dump())
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "off"})
    repo.update_company(company.id, stale)
    assert repo.get_company(company.id).outreach.auto_approve is False
    stale = CompanyIn.model_validate(repo.get_company(company.id).model_dump())
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on"})
    repo.update_company(company.id, stale)
    assert repo.get_company(company.id).outreach.auto_approve is True


def test_registrations_start_with_auto_approve_off_whatever_is_posted(client, web_settings):
    post(client, "/register", {"name": "Eager Co", "outreach.auto_approve": "true",
                               "outreach.auto_approve_hours": "1"}, page="/register")
    eager = next(c for c in repo.list_companies() if c.name == "Eager Co")
    assert (eager.outreach.auto_approve, eager.outreach.auto_approve_hours) == (False, 2)

    web_settings.password = "pw"
    web_settings.public_registration = True
    post(client, "/register", {"name": "Visitor Co", "outreach.auto_approve": "true"}, page="/register")
    visitor = next(c for c in repo.list_companies() if c.name == "Visitor Co")
    assert (visitor.status, visitor.outreach.auto_approve, visitor.outreach.auto_approve_since) == (
        "paused", False, None)


def test_the_json_api_cannot_turn_auto_approve_on_or_shorten_it(client, web_settings, company):
    web_settings.api_token = "tok"
    auth = {"Authorization": "Bearer tok"}
    url = f"/api/companies/{company.id}"
    for patch, words in (({"auto_approve": True}, "turn auto-approve on"),
                         ({"auto_approve_since": "2030-01-01T00:00:00+00:00"}, "change when auto-approve")):
        resp = client.patch(url, json={"outreach": patch}, headers=auth)
        assert resp.status_code == 403 and words in resp.json()["detail"], patch
        assert "only the user can" in resp.json()["detail"]
    assert client.patch(url, json={"outreach": {"auto_approve_hours": 99}}, headers=auth).status_code == 422
    for outreach in ({"auto_approve": True}, {"auto_approve_hours": 1}):
        created = client.post("/api/companies", json={"name": "Script Co", "outreach": outreach}, headers=auth)
        assert created.status_code == 403 and not any(c.name == "Script Co" for c in repo.list_companies())
    created = client.post("/api/companies", json={"name": "Script Co", "outreach": {"auto_approve_hours": 10}},
                          headers=auth)
    assert created.status_code == 201 and created.json()["outreach"]["auto_approve"] is False

    # On (from the dashboard): the API may turn it off or make the window longer, never shorter.
    auto_on(company.id, hours=6)
    resp = client.patch(url, json={"outreach": {"auto_approve_hours": 5}}, headers=auth)
    assert resp.status_code == 403 and "shorten the auto-approve window" in resp.json()["detail"]
    full = client.get(url, headers=auth).json()
    assert client.patch(url, json={"outreach": full["outreach"]}, headers=auth).status_code == 200
    assert client.patch(url, json={"outreach": {"auto_approve_hours": 12}}, headers=auth).status_code == 200
    assert client.patch(url, json={"outreach": {"auto_approve": False}}, headers=auth).status_code == 200
    out = repo.get_company(company.id).outreach
    assert (out.auto_approve, out.auto_approve_hours) == (False, 12)
    messages = client.get(f"/api/leads/{draft_for(company.id, 'Sara Ali').lead_id}", headers=auth).json()["messages"]
    assert messages[0]["auto_hold"] is False and messages[0]["approved_via"] == ""


def test_hold_clicked_just_after_auto_approve_takes_the_approval_back(client, company):
    base = f"/c/{company.id}"
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    auto_on(company.id)
    msg = draft_for(company.id, "Sara Ali")
    aged(msg.id, 3)
    # The page was opened while it was waiting; the scheduler approved it before the user clicked Hold.
    assert repo.auto_approve_due(company.id)["approved"] == [msg.id]
    assert [i["message_id"] for i in repo.send_queue(company.id)["items"]] == [msg.id]
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "hold", "next": f"{base}/outreach?tab=drafts"},
                page=f"{base}/outreach")
    back = repo.get_message(msg.id)
    assert (back.status, back.auto_hold, back.approved_via) == ("draft", True, "")
    assert repo.send_queue(company.id)["items"] == []  # the agent won't send it
    assert ("It had just been approved automatically. It's back in drafts and on hold: approve it yourself when it's "
            "ready.") in flashed(client, resp)
    # A message a person approved, or one already sent, is never changed by Hold.
    repo.update_message(msg.id, status="approved")
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "hold"}, page=f"{base}/outreach")
    assert "Only drafts can be put on hold" in flashed(client, resp) and repo.get_message(msg.id).status == "approved"


def test_editing_an_approved_message_says_when_auto_approve_approves_it_again(client, company):
    base = f"/c/{company.id}"
    msg = draft_for(company.id, "Sara Ali")
    repo.update_message(msg.id, status="approved")
    lead_url = f"{base}/leads/{msg.lead_id}"
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "save", "body": "Hi Sara, new text."}, page=lead_url)
    assert "Saved as a draft: you changed the approved text. Approve it again when it's ready." in flashed(client, resp)

    auto_on(company.id)
    repo.update_message(msg.id, status="approved")
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "save", "body": "Hi Sara, newer text."},
                page=lead_url)
    assert repo.get_message(msg.id).status == "draft"
    assert ("Saved as a draft: you changed the approved text. Auto-approve approves it in 2 hours unless you approve, "
            "hold or skip it first.") in flashed(client, resp)


def test_a_shorter_window_gives_waiting_drafts_the_new_window_from_now(client, company):
    base = f"/c/{company.id}"
    auto_on(company.id, hours=24)
    msg = draft_for(company.id, "Sara Ali")
    aged(msg.id, 6)
    before = repo.utcnow()
    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "1"},
                page=f"{base}/outreach")
    out = repo.get_company(company.id).outreach
    assert out.auto_approve_hours == 1 and out.auto_approve_since >= before
    page = flashed(client, resp)
    assert ("Review window saved: drafts are approved 1 hour after they're written or last edited. Drafts you already "
            "have get 1 hour from now.") in page
    assert repo.get_message(msg.id).status == "draft"  # 6 hours old, but not approved the moment it was saved
    assert repo.auto_approve_due(company.id, now=out.auto_approve_since + timedelta(hours=1))["approved"] == [msg.id]
    # A longer window keeps when it was turned on.
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "5"},
         page=f"{base}/outreach")
    assert repo.get_company(company.id).outreach.auto_approve_since == out.auto_approve_since


def test_turning_auto_approve_off_works_whatever_the_hours_field_holds(client, company):
    base = f"/c/{company.id}"
    auto_on(company.id)
    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "²"},
                page=f"{base}/outreach")
    assert resp.status_code == 303 and "must be a whole number of hours" in flashed(client, resp)
    resp = post(client, f"{base}/outreach/auto-approve", {"auto_approve": "off", "auto_approve_hours": "²"},
                page=f"{base}/outreach")
    assert resp.status_code == 303 and repo.get_company(company.id).outreach.auto_approve is False
    # Other digits are whole numbers too.
    post(client, f"{base}/outreach/auto-approve", {"auto_approve": "on", "auto_approve_hours": "٣"},
         page=f"{base}/outreach")
    assert repo.get_company(company.id).outreach.auto_approve_hours == 3
    # The agent's daily limit is read the same way.
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    resp = post(client, f"{base}/outreach/agent", {"agent_sending": "off", "agent_daily_limit": "²"},
                page=f"{base}/outreach")
    assert resp.status_code == 303 and repo.get_company(company.id).outreach.agent_sending is False
