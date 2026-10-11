"""Leftover sample data from the old demo (0.1.x's `openberry demo`): never sent to, emailed or auto-approved,
and removed safely on request (openberry.sample_cleanup)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from mcp.client import Client

from email_fakes import FROM, connect_mailbox
from openberry import repo
from openberry.mcp_server import build_server
from openberry.sample_cleanup import SAMPLE_LEAD_REASON, is_sample_lead
from sample_data import seed_sample_company

HOUR = timedelta(hours=1)
FOOTER = "Acme · Dubai\nNot interested? Reply \"unsubscribe\" and we won't email you again."


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
