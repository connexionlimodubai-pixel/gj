"""Leftover sample data from the old demo (0.1.x's `openberry demo`): never sent to, emailed or auto-approved,
and removed safely on request (openberry.sample_cleanup)."""

from __future__ import annotations

import asyncio
import html
import re
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from email_fakes import FROM, connect_mailbox
from openberry import backups, cli, db, repo, sample_cleanup
from openberry.mcp_server import build_server
from openberry.models import CompanyIn, LeadIn, SignalIn
from openberry.sample_cleanup import SAMPLE_LEAD_REASON, PlanChanged, cleanup_plan, is_sample_lead
from openberry.web.app import create_app
from sample_data import LEGACY_NAME, _company, seed_sample_company

HOUR = timedelta(hours=1)
FOOTER = "Acme · Dubai\nNot interested? Reply \"unsubscribe\" and we won't email you again."
ROOT = Path(__file__).resolve().parents[1]
CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)">')
PLAN_FIELD = re.compile(r'name="plan" value="([^"]+)"')
TABLES = ("companies", "leads", "lead_keys", "signals", "messages", "scan_runs", "place_ids", "email_events",
          "email_suppressions", "api_usage")


def legacy_company(**outreach: Any) -> int:
    """The rows 0.1.x's demo wrote, with the sending switches the user may have turned on since."""
    company_id = seed_sample_company(legacy_demo=True)
    if outreach:
        repo.update_company(company_id, {"outreach": outreach})
    return company_id


def drafts(company_id: int) -> list[int]:
    return sorted(m.id for m in repo.list_messages(company_id, status="draft"))


# --------------------------------------------------------------------------------------
# Leftover sample leads are never sent to (3.3)
# --------------------------------------------------------------------------------------

def test_the_legacy_rows_are_what_the_old_demo_wrote():
    company_id = legacy_company()
    company = repo.get_company(company_id)
    assert company.name == "Demo: Desert Line Chauffeurs" and company.contact_email == "demo@example.com"
    leads, total = repo.list_leads(company_id, limit=100)
    assert total == 13 and all(is_sample_lead(lead) for lead in leads)
    assert all(lead.linkedin_url.endswith("-demo") for lead in leads if lead.kind == "person")
    assert [run.trigger for run in repo.list_scan_runs(company_id)] == ["demo"]
    assert len(drafts(company_id)) == 2
    # The test-only sample company (the default) is ordinary data: it is not sample data from the old demo.
    sample_id = seed_sample_company(name="Sample Co")
    assert not any(is_sample_lead(lead) for lead in repo.list_leads(sample_id, limit=100)[0])
    assert [run.trigger for run in repo.list_scan_runs(sample_id)] == ["cli"]


async def test_agent_sending_never_queues_or_confirms_a_sample_lead():
    now = repo.utcnow()
    company_id = legacy_company(agent_sending=True)
    ids = drafts(company_id)
    for message_id in ids:
        repo.update_message(message_id, status="approved")  # approved by hand, as a user trying the demo might
    queue = repo.send_queue(company_id, now=now)
    assert queue["enabled"] is True and queue["blocked_reason"] == ""
    assert queue["items"] == [] and queue["eligible_total"] == 0
    assert {s["message_id"]: s["reason"] for s in queue["skipped"]} == {i: SAMPLE_LEAD_REASON for i in ids}
    async with Client(build_server()) as client:
        result = await client.call_tool("get_send_queue", {"company_id": company_id})
    assert not result.is_error and result.structured_content["items"] == []
    for message_id in ids:
        with pytest.raises(ValueError, match="sample data from the old demo, not a real person"):
            repo.confirm_agent_sent(message_id, now=now)
        assert repo.get_message(message_id).status == "approved" and repo.get_message(message_id).sent_at is None


def test_auto_approve_leaves_sample_drafts_alone_and_flags_them():
    now = repo.utcnow()
    company_id = legacy_company(agent_sending=True, auto_approve=True, auto_approve_hours=1,
                                auto_approve_since=repo.iso(now - timedelta(days=30)))
    ids = drafts(company_id)
    result = repo.auto_approve_due(company_id, now=now + 3 * HOUR)
    assert result["approved"] == [] and result["blocked"] == 2
    for message_id in ids:
        message = repo.get_message(message_id)
        assert message.status == "draft" and message.auto_blocked
    states = repo.auto_approve_states(repo.get_company(company_id), [repo.get_message(i) for i in ids],
                                      now=now + 3 * HOUR)
    assert {state["reason"] for state in states.values()} == {SAMPLE_LEAD_REASON}
    assert repo.send_queue(company_id, now=now + 3 * HOUR)["skipped"] == []  # nothing approved, nothing to send


def test_email_sending_skips_a_sample_lead_even_after_the_user_gave_it_an_address(settings, offline_mail):
    now = repo.utcnow()
    connect_mailbox(settings)
    repo.save_inbox_check(FROM, True, now=now)
    company_id = legacy_company(email_sending=True, email_footer=FOOTER)
    person = next(lead for lead in repo.list_leads(company_id, kind="person", limit=100)[0])
    repo.update_lead(person.id, {"email": "aisha@falcon-ridge.example"})  # a touched sample lead stays sample data
    message = repo.create_message(person.id, "Hi Aisha,\n\nCorporate chauffeur account?\n\nSam", channel="email",
                                  subject="Chauffeurs for your roadshow", status="approved", generated_by="claude")
    queue = repo.email_queue(company_id, now=now)
    assert queue["items"] == [] and queue["eligible_total"] == 0
    assert {s["message_id"]: s["reason"] for s in queue["skipped"]} == {message.id: SAMPLE_LEAD_REASON}
    assert repo.claim_next_email("worker", FROM, now=now) is None
    assert offline_mail.sent == []


# --------------------------------------------------------------------------------------
# What may be removed (4.1)
# --------------------------------------------------------------------------------------

def later(monkeypatch: pytest.MonkeyPatch, minutes: int = 5) -> None:
    """Edits from here on are stamped a few minutes later (timestamps have one-second resolution)."""
    real = repo.utcnow
    monkeypatch.setattr(repo, "utcnow", lambda: real() + timedelta(minutes=minutes))


def lead_named(company_id: int, name: str) -> int:
    return next(lead.id for lead in repo.list_leads(company_id, limit=100)[0]
                if name in (lead.full_name, lead.lead_company) and (lead.full_name or lead.kind == "account"))


def counts() -> dict[str, int]:
    with db.connect() as c:
        return {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}


def test_an_untouched_sample_company_can_go_whole(monkeypatch):
    company_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    repo.rescore_company(company_id)  # rescoring and hot-lead alerts aren't edits
    repo.claim_new_hot_leads(company_id)
    plan = cleanup_plan(company_id)
    assert plan is not None and plan.profile_edited is False
    assert plan.sample_lead_ids == sorted(lead.id for lead in repo.list_leads(company_id, limit=100)[0])
    assert len(plan.sample_lead_ids) == 13
    assert plan.touched_demo_lead_ids == [] and plan.other_lead_ids == [] and plan.sent_messages == []
    assert plan.whole_company_safe is True
    assert (plan.sample_signals, plan.sample_drafts) == (14, 2)
    assert plan.counts == {"leads": 13, "messages": 2, "signals": 14, "scan_runs": 1}
    assert [f["label"] for f in plan.sample_fields] == ["Name", "Contact name", "Contact email", "Website",
                                                         "Sender name", "Signature"]
    assert {f["label"]: f["value"] for f in plan.sample_fields}["Contact email"] == "demo@example.com"


def test_a_note_an_approval_and_an_added_lead_keep_the_company(monkeypatch):
    company_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    aisha, priya = lead_named(company_id, "Aisha Rahman"), lead_named(company_id, "Priya Nair")
    repo.update_lead(priya, {"notes": "call her"})
    repo.update_message(repo.list_messages(company_id, lead_id=aisha)[0].id, status="approved")
    added, _ = repo.upsert_lead(company_id, LeadIn(full_name="Real HN user", source="hackernews",
                                                   profile_url="https://news.ycombinator.com/user?id=x"))
    plan = cleanup_plan(company_id)
    assert plan.touched_demo_lead_ids == sorted([aisha, priya]) == [1, 3]
    assert plan.other_lead_ids == [added.id] == [14]
    assert plan.whole_company_safe is False and plan.profile_edited is False
    assert len(plan.sample_lead_ids) == 11


def test_a_renamed_or_saved_profile_counts_as_edited(monkeypatch):
    company_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    repo.update_company(company_id, {"name": "Connexion Limo"})
    plan = cleanup_plan(company_id)
    assert plan.profile_edited is True and plan.whole_company_safe is False
    assert "Name" not in {f["label"] for f in plan.sample_fields}  # it no longer holds the sample value
    other = seed_sample_company(legacy_demo=True)
    later(monkeypatch, minutes=10)
    repo.update_company(other, {"status": "paused"})  # any save: the profile form, Pause, the Outreach switches
    assert cleanup_plan(other).profile_edited is True


@pytest.mark.parametrize("change", ["email", "tags", "status", "assessment", "signal", "edited_draft", "held_draft"])
def test_any_change_to_a_sample_lead_keeps_it(change, monkeypatch):
    company_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    aisha = lead_named(company_id, "Aisha Rahman")
    draft = repo.list_messages(company_id, lead_id=aisha)[0]
    if change == "email":
        repo.update_lead(aisha, {"email": "aisha@falcon-ridge.example"})
    elif change == "tags":
        repo.update_lead(aisha, {"tags": ["vip"]})
    elif change == "status":
        repo.update_lead(aisha, {"status": "qualified"})
    elif change == "assessment":
        repo.set_ai_assessment(aisha, 80, "good fit")
    elif change == "signal":  # a scan found her for real: that signal isn't the old version's to delete
        repo.add_signal(company_id, SignalIn(type="keyword_mention", title="Real post", source="hackernews",
                                             url="https://news.ycombinator.com/item?id=1"), lead_id=aisha)
    elif change == "edited_draft":
        repo.update_message(draft.id, body="Hi Aisha, a new text")
    else:
        repo.set_auto_hold(draft.id)
    plan = cleanup_plan(company_id)
    assert plan.touched_demo_lead_ids == [aisha] and aisha not in plan.sample_lead_ids
    assert plan.whole_company_safe is False


def test_only_the_marker_run_makes_a_sample_company():
    sample_id = seed_sample_company(name="Sample Co")  # the test-only sample company: ordinary data
    renamed = repo.create_company(CompanyIn(name=LEGACY_NAME))  # the old name alone means nothing
    assert cleanup_plan(sample_id) is None and cleanup_plan(renamed.id) is None
    assert sample_cleanup.sample_company_ids() == set()
    legacy = seed_sample_company(legacy_demo=True)
    assert sample_cleanup.sample_company_ids() == {legacy}


def test_profile_fingerprints_match_what_the_old_version_wrote():
    legacy = CompanyIn.model_validate(_company(legacy_demo=True, name="", status="active"))
    for path, digest in sample_cleanup.PROFILE_FINGERPRINTS.items():
        value: Any = legacy
        for part in path.split("."):
            value = getattr(value, part)
        assert sample_cleanup.fingerprint(value) == digest, path
    assert set(sample_cleanup.PROFILE_FINGERPRINTS) == set(sample_cleanup.PROFILE_LABELS)


# --------------------------------------------------------------------------------------
# The scheduler leaves an untouched sample company alone (4.2)
# --------------------------------------------------------------------------------------

def test_scheduled_scans_skip_the_untouched_sample_company(company, monkeypatch):
    company_id = seed_sample_company(legacy_demo=True)
    soon = repo.utcnow() + timedelta(hours=25)
    assert [c.id for c in repo.companies_due_for_scan(soon)] == [company.id]
    later(monkeypatch)
    repo.update_company(company_id, {"name": "Connexion Limo"})  # the user made it theirs: scans resume
    assert sorted(c.id for c in repo.companies_due_for_scan(soon)) == sorted([company.id, company_id])


# --------------------------------------------------------------------------------------
# Removal (4.3)
# --------------------------------------------------------------------------------------

def other_company_with_kept_rows(sample_id: int) -> int:
    """A real company with one lead, plus the install-wide rows the removal must keep, and the sample company's own
    Google Maps and email history (they cascade with it)."""
    real = repo.create_company(CompanyIn(name="Connexion Limo Dubai"))
    repo.upsert_lead(real.id, LeadIn(full_name="Pat Real", linkedin_url="https://www.linkedin.com/in/pat-real"))
    repo.add_suppression("optout@acme-real.ae", "opt_out", company_id=sample_id)
    repo.reserve_api_call("google_places_search", 100)
    repo.record_place_ids(sample_id, {"ChIJsample": False})
    with db.connect() as c:
        c.execute("INSERT INTO email_events (company_id, kind, created_at) VALUES (?, 'paused', ?)",
                  (sample_id, repo.iso()))
    return real.id


def test_removing_an_untouched_sample_company_deletes_it_after_a_backup(monkeypatch, settings):
    sample_id = seed_sample_company(legacy_demo=True)
    real_id = other_company_with_kept_rows(sample_id)
    before = counts()
    assert before == {"companies": 2, "leads": 14, "lead_keys": before["lead_keys"], "signals": 14, "messages": 2,
                      "scan_runs": 1, "place_ids": 1, "email_events": 1, "email_suppressions": 1, "api_usage": 1}
    plan = cleanup_plan(sample_id)
    done = sample_cleanup.remove(sample_id, plan.token)
    assert done.company_deleted and done.removed_leads == 13
    assert done.backup.parent == settings.db_path.parent / "backups" and done.backup.name.endswith("-before-delete.db")
    with closing(sqlite3.connect(done.backup)) as copy:  # taken under the write lock, before the delete
        assert copy.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2
        assert copy.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 14
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [s.path for s in backups.list_snapshots()] == [done.backup]
    after = counts()
    assert after == {"companies": 1, "leads": 1, "lead_keys": after["lead_keys"], "signals": 0, "messages": 0,
                     "scan_runs": 0, "place_ids": 0, "email_events": 1, "email_suppressions": 1, "api_usage": 1}
    assert after["lead_keys"] == len(repo.lead_identity_keys(repo.get_lead(lead_named(real_id, "Pat Real")),
                                                             "person"))
    with db.connect() as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT company_id FROM email_suppressions").fetchone()[0] is None
        # The email history outlives the company (schema 6), unlinked from it, so "never email twice" still holds.
        assert c.execute("SELECT company_id FROM email_events").fetchone()[0] is None
    assert [c.id for c in repo.list_companies()] == [real_id]


def test_a_failed_backup_deletes_nothing(monkeypatch):
    def backup_to(dest: Path, source: Path | None = None) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(db, "backup_to", backup_to, raising=False)
    sample_id = seed_sample_company(legacy_demo=True)
    before = counts()
    with pytest.raises(OSError, match="No space"):
        sample_cleanup.remove(sample_id, cleanup_plan(sample_id).token)
    assert counts() == before


def test_otherwise_only_the_untouched_sample_leads_go(monkeypatch):
    sample_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    aisha, priya = lead_named(sample_id, "Aisha Rahman"), lead_named(sample_id, "Priya Nair")
    repo.update_lead(priya, {"notes": "call her"})
    repo.update_message(repo.list_messages(sample_id, lead_id=aisha)[0].id, status="approved")
    added, _ = repo.upsert_lead(sample_id, LeadIn(full_name="Real HN user", source="hackernews",
                                                  profile_url="https://news.ycombinator.com/user?id=x"))
    plan = cleanup_plan(sample_id)
    copies: list[Path] = []
    monkeypatch.setattr(db, "backup_to", lambda dest, source=None: copies.append(dest))
    done = sample_cleanup.remove(sample_id, plan.token)
    assert not done.company_deleted and (done.removed_leads, done.kept_leads) == (11, 3)
    assert copies == [] and done.backup is None  # nothing but sample leads was deleted
    assert sorted(lead.id for lead in repo.list_leads(sample_id, limit=100)[0]) == sorted([aisha, priya, added.id])
    assert repo.get_company(sample_id).name == LEGACY_NAME  # the profile stays
    assert sorted(m.lead_id for m in repo.list_messages(sample_id)) == sorted([aisha, priya])  # their drafts stay
    assert cleanup_plan(sample_id) is None and repo.list_scan_runs(sample_id) == []  # no marker, no banner
    signals, _ = repo.list_signals(sample_id, limit=100, include_account=True)
    assert {s.lead_id for s in signals} <= {aisha, priya, added.id}


def test_a_plan_that_changed_removes_nothing(monkeypatch):
    sample_id = seed_sample_company(legacy_demo=True)
    token = cleanup_plan(sample_id).token
    later(monkeypatch)
    repo.update_lead(lead_named(sample_id, "Omar Haddad"), {"notes": "met him at GITEX"})
    before = counts()
    with pytest.raises(PlanChanged) as exc:
        sample_cleanup.remove(sample_id, token)
    assert exc.value.plan is not None and exc.value.plan.token != token
    assert counts() == before
    with pytest.raises(PlanChanged) as exc:
        sample_cleanup.remove(repo.create_company(CompanyIn(name="Normal")).id, token)
    assert exc.value.plan is None


# --------------------------------------------------------------------------------------
# The dashboard: banner, confirmation page, removal (4.3)
# --------------------------------------------------------------------------------------

@pytest.fixture
def client(settings):
    settings.http_mcp_enabled = False
    settings.allowed_hosts = ["testserver"]
    with TestClient(create_app(settings)) as c:
        yield c


def csrf(client: TestClient, path: str) -> str:
    match = CSRF_META.search(client.get(path).text)
    assert match, path
    return match.group(1)


def confirm(client: TestClient, company_id: int, plan: str | None = None):
    page = client.get(f"/c/{company_id}/sample").text
    data = {"csrf_token": CSRF_META.search(page).group(1), "plan": plan or PLAN_FIELD.search(page).group(1)}
    return client.post(f"/c/{company_id}/sample", data=data, follow_redirects=False)


def test_the_banner_shows_on_the_sample_company_only(client, company):
    sample_id = seed_sample_company(legacy_demo=True)
    for path in (f"/c/{sample_id}", f"/c/{sample_id}/leads", f"/c/{sample_id}/settings"):
        page = client.get(path).text
        assert 'class="sample-banner"' in page and sample_cleanup.BANNER_TEXT in page, path
        assert f'href="/c/{sample_id}/sample">' in page and "Delete sample company" in page
    for path in (f"/c/{company.id}", f"/c/{company.id}/leads", "/companies", "/help"):
        assert 'class="sample-banner"' not in client.get(path).text, path
    board = client.get("/companies").text
    assert f'<a class="pill pill-sample" href="/c/{sample_id}/sample"' in board and board.count("pill-sample") == 1
    assert client.get(f"/c/{company.id}/sample").status_code == 404


def test_help_prompts_name_a_real_company_not_the_sample_one(client, company):
    sample_id = seed_sample_company(legacy_demo=True)  # newer, so first in the list
    page = client.get("/help").text
    assert f"company {company.id}&#39;s profile" in page and f"company {sample_id}" not in page
    repo.delete_company(company.id)
    assert "onboard_company" in client.get("/help").text


def test_the_confirmation_page_lists_what_goes_and_what_stays(client, monkeypatch):
    sample_id = seed_sample_company(legacy_demo=True)
    page = client.get(f"/c/{sample_id}/sample").text
    assert "Delete the sample company" in page and "13 sample leads, 14 signals and 2 drafts" in page
    assert "Aisha Rahman" in page and "the email opt-outs and bounces" in page
    assert "A backup of your data is saved first." in page
    assert sample_cleanup.BANNER_TEXT in page and 'class="sample-banner"' not in page  # the lede, not the banner
    later(monkeypatch)
    repo.update_lead(lead_named(sample_id, "Priya Nair"), {"notes": "call her"})
    added, _ = repo.upsert_lead(sample_id, LeadIn(full_name="Pat Real", source="manual",
                                                  linkedin_url="https://www.linkedin.com/in/pat-real"))
    page = client.get(f"/c/{sample_id}/sample").text
    assert "Remove the sample data" in page and "Remove 12 sample leads" in page
    assert "Sample leads that changed (1)" in page and f'href="/c/{sample_id}/leads/{added.id}"' in page
    assert "Other leads (1)" in page and "These fields still hold sample values" in page
    assert "demo@example.com" in page and "Company profile → Delete company" in page


def test_confirming_deletes_the_untouched_sample_company(client, company):
    sample_id = seed_sample_company(legacy_demo=True)
    resp = confirm(client, sample_id)
    assert resp.status_code == 303 and resp.headers["location"] == "/companies"
    assert [c.id for c in repo.list_companies()] == [company.id]
    board = client.get("/companies").text
    assert f"Deleted the sample company {LEGACY_NAME}." in board
    again = client.post(f"/c/{sample_id}/sample", data={"csrf_token": csrf(client, "/companies"), "plan": "x"},
                        follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/companies"  # clicked twice


def test_confirming_removes_only_the_untouched_leads_when_something_changed(client, monkeypatch):
    sample_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    priya = lead_named(sample_id, "Priya Nair")
    repo.update_lead(priya, {"notes": "call her"})
    resp = confirm(client, sample_id)
    assert resp.status_code == 303 and resp.headers["location"] == f"/c/{sample_id}"
    page = client.get(f"/c/{sample_id}").text
    assert "Removed 12 untouched sample leads. The company and its other 1 lead stay." in page
    assert 'class="sample-banner"' not in page  # the banner is gone
    assert [lead.id for lead in repo.list_leads(sample_id, limit=100)[0]] == [priya]
    assert client.get(f"/c/{sample_id}/sample").status_code == 404


def test_a_stale_confirmation_gets_409_and_removes_nothing(client, monkeypatch):
    sample_id = seed_sample_company(legacy_demo=True)
    page = client.get(f"/c/{sample_id}/sample").text
    later(monkeypatch)
    repo.upsert_lead(sample_id, LeadIn(full_name="Pat Real", linkedin_url="https://www.linkedin.com/in/pat-real"))
    before = counts()
    resp = confirm(client, sample_id, plan=PLAN_FIELD.search(page).group(1))
    assert resp.status_code == 409 and "Something changed since you opened this page" in resp.text
    assert "Remove 13 sample leads" in resp.text  # the new plan, to confirm again
    assert counts() == before


def test_removal_needs_a_csrf_token(client):
    sample_id = seed_sample_company(legacy_demo=True)
    token = cleanup_plan(sample_id).token
    assert client.post(f"/c/{sample_id}/sample", data={"plan": token}).status_code == 403
    assert repo.find_company(sample_id) is not None


async def test_claude_is_told_which_company_is_the_sample(company):
    sample_id = seed_sample_company(legacy_demo=True)
    async with Client(build_server()) as client:
        result = await client.call_tool("list_companies", {})
    rows = {row["id"]: row for row in result.structured_content["companies"]}
    assert rows[sample_id]["note"] == sample_cleanup.COMPANY_NOTE and "note" not in rows[company.id]


def list_companies_note(company_id: int) -> str | None:
    """The note Claude gets with this company from MCP list_companies (None without one)."""
    async def call() -> Any:
        async with Client(build_server()) as mcp:
            return await mcp.call_tool("list_companies", {})
    rows = asyncio.run(call()).structured_content["companies"]
    return next(row.get("note") for row in rows if row["id"] == company_id)


def sample_banner(client: TestClient, company_id: int) -> str:
    page = client.get(f"/c/{company_id}/leads").text
    return html.unescape(re.search(r'<div class="sample-banner".*?</div>', page, re.S).group(0))


def test_a_sample_company_the_user_made_their_own_isnt_called_fictional(client, monkeypatch, capsys):
    """UPG-4: the user turned the old demo company into their own: a new profile, and leads they added. Claude's note
    and the banner say only which sample leads it still holds (also the ones changed since), and the button removes
    those, not the company. With none left, Claude gets no note and the banner only offers to remove the marker."""
    sample_id = seed_sample_company(legacy_demo=True)
    assert list_companies_note(sample_id) == sample_cleanup.COMPANY_NOTE  # untouched: fictional, leave it alone
    assert f"{sample_cleanup.BANNER_TEXT} Nothing is ever sent to them." in sample_banner(client, sample_id)
    assert "Delete sample company" in sample_banner(client, sample_id)
    untouched = f'title="{sample_cleanup.BANNER_TEXT} Nothing is ever sent to them.">Sample</a>'
    assert untouched in html.unescape(client.get("/companies").text)  # the Registration board's pill
    later(monkeypatch)
    repo.update_company(sample_id, {"name": "Gulf Executive Cars", "website": "https://gulfexec.example"})
    repo.upsert_lead(sample_id, LeadIn(full_name="Pat Real", source="claude",
                                       linkedin_url="https://www.linkedin.com/in/pat-real"))
    repo.update_lead(lead_named(sample_id, "Priya Nair"), {"notes": "call her"})  # still a sample lead
    text = "This company still holds 13 sample leads from the old demo. Sample leads are never contacted."
    note, shown = list_companies_note(sample_id), sample_banner(client, sample_id)
    assert note == f"{text} The user can remove them with the banner on its dashboard."
    assert text in shown and f'href="/c/{sample_id}/sample">' in shown and "Remove sample leads" in shown
    for words in ("fictional", "Don't work on it", "Nothing is ever sent", "Delete sample company"):
        assert words not in note and words not in shown, words
    page = html.unescape(client.get(f"/c/{sample_id}/sample").text)  # the removal page the button opens
    assert f"{text} Nothing is removed until you click" in page and "Remove 12 sample leads" in page
    assert "fictional" not in page
    board = html.unescape(client.get("/companies").text)
    assert f'title="{text}">Sample</a>' in board and "fictional" not in board
    cli.main(["remove-sample", "--dry-run"])
    out = capsys.readouterr().out
    assert text in out and "fictional" not in out
    for lead in repo.list_leads(sample_id, limit=100)[0]:
        if is_sample_lead(lead):
            repo.delete_lead(lead.id)
    assert list_companies_note(sample_id) is None
    shown = sample_banner(client, sample_id)
    assert "No sample leads from the old demo are left in this company." in shown and "Remove sample marker" in shown
    assert "0 sample leads" not in shown and "Remove the sample marker" in client.get(f"/c/{sample_id}/sample").text


def test_leads_added_to_the_sample_company_dont_make_it_the_users(client, monkeypatch):
    """0.1.x scanned the demo company like any other, so it may hold leads a scan found. Its profile is still the
    fictional one: Claude is still told to leave it alone, and the banner still says it is the sample company."""
    sample_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    repo.upsert_lead(sample_id, LeadIn(full_name="Real HN user", source="hackernews",
                                       profile_url="https://news.ycombinator.com/user?id=x"))
    assert list_companies_note(sample_id) == sample_cleanup.COMPANY_NOTE
    shown = sample_banner(client, sample_id)
    assert f"{sample_cleanup.BANNER_TEXT} Nothing is ever sent to them." in shown
    assert "Remove sample leads" in shown  # the company holds a lead that isn't sample data: it stays


# --------------------------------------------------------------------------------------
# openberry remove-sample (4.4)
# --------------------------------------------------------------------------------------

def test_cli_without_a_sample_company(company, capsys):
    cli.main(["remove-sample"])
    cli.main(["remove-sample", "--company", str(company.id), "--yes"])
    assert capsys.readouterr().out.splitlines() == ["No sample company found."] * 2
    assert repo.find_company(company.id) is not None


def test_cli_dry_run_changes_nothing(capsys):
    sample_id = seed_sample_company(legacy_demo=True)
    before = counts()
    cli.main(["remove-sample", "--dry-run"])
    out = capsys.readouterr().out
    assert f"Company {sample_id}: {LEGACY_NAME}." in out and "the whole company goes (13 leads" in out
    assert "Dry run: nothing was changed." in out and counts() == before


def test_cli_asks_first_and_removes_with_yes(capsys, monkeypatch):
    sample_id = seed_sample_company(legacy_demo=True)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError))  # no terminal
    cli.main(["remove-sample"])
    assert "Nothing was changed. Add --yes" in capsys.readouterr().out and repo.find_company(sample_id)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cli.main(["remove-sample", "--company", str(sample_id)])
    assert f"Deleted {LEGACY_NAME}." in capsys.readouterr().out and repo.find_company(sample_id) is None


def test_cli_removes_only_untouched_leads_when_something_changed(capsys, monkeypatch):
    sample_id = seed_sample_company(legacy_demo=True)
    later(monkeypatch)
    repo.update_lead(lead_named(sample_id, "Priya Nair"), {"notes": "call her"})
    cli.main(["remove-sample", "--yes"])
    out = capsys.readouterr().out
    assert "Kept: the company profile; 1 sample lead that changed since" in out
    assert "Profile fields that still hold sample values: Name: " in out
    assert f"Removed 12 sample leads. {LEGACY_NAME} and its other lead stay." in out


# --------------------------------------------------------------------------------------
# Release check: the product no longer talks about the demo
# --------------------------------------------------------------------------------------

TEXT_FILES = (".py", ".html", ".md", ".css", ".js", ".txt", ".json", ".svg")


def text_files(*paths: str) -> list[Path]:
    out = []
    for path in paths:
        base = ROOT / path
        for file in [base] if base.is_file() else sorted(base.rglob("*")):
            if file.is_file() and file.suffix in TEXT_FILES and "__pycache__" not in file.parts:
                out.append(file)
    return out


def test_only_sample_cleanup_still_says_demo():
    hits = {}
    for file in text_files("src/openberry"):
        lines = [line.strip() for line in file.read_text(encoding="utf-8").splitlines() if "demo" in line.lower()]
        if lines:
            hits[file.relative_to(ROOT).as_posix()] = lines
    assert set(hits) <= {"src/openberry/sample_cleanup.py", "src/openberry/web/ui.py"}
    assert hits.get("src/openberry/web/ui.py", [""])[0].startswith('"demo": "Sample (old demo)"')
    assert len(hits.get("src/openberry/web/ui.py", [])) <= 1
    for file in text_files("src", "docs", "README.md"):
        assert "Desert Line" not in file.read_text(encoding="utf-8"), file
