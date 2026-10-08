"""Web dashboard and JSON API tests (FastAPI TestClient against a temporary SQLite database)."""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
import sys
import time
import types
from contextlib import asynccontextmanager
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
    assert client.get("/healthz").json() == {"ok": True}
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
    assert "Omar Haddad" in drafts and "/ 300 characters" in drafts

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
        assert c.get("/healthz").json() == {"ok": True}


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
