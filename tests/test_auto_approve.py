"""Auto-approve: drafts nobody edits, holds or skips are approved after a review window, and everything it leaves
alone. The dashboard's card and buttons are tested in test_web.py."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from mcp.client import Client
from pydantic import ValidationError

from openberry import db, repo, scheduler, services
from openberry.mcp_server import build_server
from openberry.models import Company, CompanyIn, LeadIn, Message, OutreachConfig

HOUR = timedelta(hours=1)
NOTE = "Hi, saw your post about airport transfers in Dubai. Happy to connect!"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def turn_on(company: Company, hours: int = 2, since: datetime | None = None, **outreach: Any) -> Company:
    """What the dashboard does when the user turns auto-approve on (`since` defaults to long ago)."""
    since = since or repo.utcnow() - timedelta(days=30)
    return repo.update_company(company.id, {"outreach": {"auto_approve": True, "auto_approve_hours": hours,
                                                         "auto_approve_since": repo.iso(since), **outreach}})


_people = iter(range(1, 10_000))


def person(company: Company, name: str = "", **fields: Any) -> int:
    n = next(_people)
    data = {"full_name": name or f"Person {n}", "title": "Travel Manager", "lead_company": f"Client {n}",
            "linkedin_url": f"https://www.linkedin.com/in/auto-person-{n}", **fields}
    lead, _ = repo.upsert_lead(company.id, LeadIn(**data))
    return lead.id


def written_at(message_id: int, when: datetime) -> None:
    """Pretend the message was written (and last changed) at `when`."""
    stamp = repo.iso(when)
    with db.connect() as c:
        c.execute("UPDATE messages SET created_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, message_id))


def draft(lead_id: int, body: str = NOTE, channel: str = "linkedin_connect", step: int = 1, subject: str = "",
          hours_ago: float = 0) -> Message:
    msg = repo.create_message(lead_id, body, channel=channel, step=step, subject=subject, generated_by="claude")
    if hours_ago:
        written_at(msg.id, repo.utcnow() - timedelta(hours=hours_ago))
    return repo.get_message(msg.id)


def status(message_id: int) -> str:
    return repo.get_message(message_id).status


def state(company: Company, message_id: int, now: datetime | None = None) -> dict[str, Any]:
    company = repo.get_company(company.id)
    return repo.auto_approve_states(company, [repo.get_message(message_id)], now=now)[message_id]


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


def test_settings_default_off_with_a_two_hour_window_of_1_to_72_hours(company):
    cfg = OutreachConfig()
    assert (cfg.auto_approve, cfg.auto_approve_hours, cfg.auto_approve_since) == (False, 2, None)
    for hours in (0, 73, -1):
        with pytest.raises(ValidationError):
            OutreachConfig(auto_approve_hours=hours)
    assert OutreachConfig(auto_approve_hours=1).auto_approve_hours == 1
    assert OutreachConfig(auto_approve_hours=72).auto_approve_hours == 72
    naive = OutreachConfig(auto_approve_since=datetime(2026, 10, 1, 9, 30)).auto_approve_since
    assert naive == datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    assert "Drafts are never sent" in OutreachConfig.model_fields["mode"].description
    assert "Nothing is ever sent automatically" not in OutreachConfig.model_fields["mode"].description

    # Off: nothing is ever approved, however old the draft.
    msg = draft(person(company), hours_ago=24 * 30)
    assert repo.auto_approve_due(company.id) == {"approved": [], "waiting": 0, "held": 0, "blocked": 0,
                                                 "next_at": None}
    assert status(msg.id) == "draft" and state(company, msg.id) == {"state": "off", "at": None, "reason": ""}


# --------------------------------------------------------------------------------------
# The review window
# --------------------------------------------------------------------------------------


def test_a_draft_is_approved_once_its_window_has_passed(company):
    turn_on(company, hours=2)
    msg = draft(person(company), hours_ago=1)
    written = repo.get_message(msg.id).updated_at
    now = repo.utcnow()
    result = repo.auto_approve_due(company.id, now=now)
    assert result["approved"] == [] and result["waiting"] == 1
    assert result["next_at"] == repo.iso(written + 2 * HOUR)
    assert state(company, msg.id, now) == {"state": "waiting", "at": written + 2 * HOUR, "reason": ""}

    later = written + 2 * HOUR
    assert repo.auto_approve_due(company.id, now=later)["approved"] == [msg.id]
    approved = repo.get_message(msg.id)
    assert (approved.status, approved.approved_via, approved.auto_hold) == ("approved", "auto", False)
    assert approved.updated_at == later and approved.body == NOTE  # exactly the text that waited
    assert repo.auto_approve_due(company.id, now=later + HOUR)["approved"] == []  # once


def test_turning_it_on_gives_every_draft_the_full_window_from_then(company):
    backlog = [draft(person(company), hours_ago=24 * 7) for _ in range(3)]
    switched_on = repo.utcnow()
    turn_on(company, hours=4, since=switched_on)
    result = repo.auto_approve_due(company.id, now=switched_on + HOUR)
    assert result["approved"] == [] and result["waiting"] == 3  # never a backlog at once
    assert state(company, backlog[0].id)["at"] == switched_on + 4 * HOUR
    assert sorted(repo.auto_approve_due(company.id, now=switched_on + 4 * HOUR)["approved"]) == [
        m.id for m in backlog]
    # A draft written after it was turned on waits from when it was written.
    fresh = draft(person(company))
    assert state(company, fresh.id)["at"] == fresh.updated_at + 4 * HOUR


def test_an_edit_restarts_the_window(company):
    turn_on(company, hours=2)
    msg = draft(person(company), hours_ago=3)
    repo.update_message(msg.id, body="Hi, saw your post about airport transfers. Happy to connect!")
    edited = repo.get_message(msg.id).updated_at
    assert repo.auto_approve_due(company.id, now=edited + HOUR)["approved"] == []
    assert repo.auto_approve_due(company.id, now=edited + 2 * HOUR)["approved"] == [msg.id]

    # Editing the text of an auto-approved message makes it a draft again, not held: it waits a new window.
    repo.update_message(msg.id, body="Hi again, happy to connect!")
    again = repo.get_message(msg.id)
    assert (again.status, again.approved_via, again.auto_hold) == ("draft", "", False)
    assert repo.auto_approve_due(company.id, now=again.updated_at + HOUR)["approved"] == []
    assert repo.auto_approve_due(company.id, now=again.updated_at + 2 * HOUR)["approved"] == [msg.id]


# --------------------------------------------------------------------------------------
# Hold, release, back to drafts
# --------------------------------------------------------------------------------------


def test_a_held_draft_is_never_auto_approved_and_release_restarts_its_window(company):
    turn_on(company)
    msg = draft(person(company), hours_ago=5)
    held = repo.set_auto_hold(msg.id, True)
    assert held.auto_hold is True and held.updated_at == msg.updated_at
    result = repo.auto_approve_due(company.id, now=repo.utcnow() + 100 * HOUR)
    assert result["approved"] == [] and result["held"] == 1 and status(msg.id) == "draft"
    assert state(company, msg.id)["state"] == "held"

    released = repo.set_auto_hold(msg.id, False)
    assert released.auto_hold is False and released.updated_at > msg.updated_at  # a new window from now
    assert repo.auto_approve_due(company.id)["approved"] == []
    assert repo.auto_approve_due(company.id, now=released.updated_at + 2 * HOUR)["approved"] == [msg.id]

    # Only drafts are held or released; releasing a draft that isn't held changes nothing.
    with pytest.raises(ValueError, match="only drafts can be held"):
        repo.set_auto_hold(msg.id, True)
    other = draft(person(company), hours_ago=1)
    assert repo.set_auto_hold(other.id, False).updated_at == other.updated_at


def test_back_to_drafts_holds_but_a_text_edit_does_not(company):
    turn_on(company)
    by_hand = repo.create_message(person(company), NOTE, channel="linkedin_connect", status="approved")
    back = repo.update_message(by_hand.id, status="draft")  # the dashboard's Back to drafts, or Claude
    assert (back.status, back.auto_hold, back.approved_via) == ("draft", True, "")
    assert repo.auto_approve_due(company.id, now=repo.utcnow() + 100 * HOUR)["approved"] == []

    edited = repo.create_message(person(company), NOTE, channel="linkedin_connect", status="approved")
    after = repo.update_message(edited.id, body="Hi, happy to connect!")
    assert (after.status, after.auto_hold) == ("draft", False)

    # Setting a draft or a skipped message to draft holds nothing.
    plain = draft(person(company))
    assert repo.update_message(plain.id, status="draft").auto_hold is False
    skipped = repo.update_message(draft(person(company)).id, status="skipped")
    assert repo.update_message(skipped.id, status="draft").auto_hold is False


def test_a_lead_that_leaves_the_pipeline_or_gets_a_new_profile_holds_its_unapproved_messages(company):
    turn_on(company)
    lead_id = person(company)
    msg = repo.create_message(lead_id, NOTE, channel="linkedin_connect", status="approved")
    repo.update_lead(lead_id, {"status": "lost"})
    repo.update_lead(lead_id, {"status": "contacted"})  # set back later: the old approval must not come back
    lapsed = repo.get_message(msg.id)
    assert (lapsed.status, lapsed.auto_hold) == ("draft", True)
    assert repo.auto_approve_due(company.id, now=repo.utcnow() + 100 * HOUR)["approved"] == []

    other = person(company)
    msg = repo.create_message(other, NOTE, channel="linkedin_connect", status="approved")
    repo.update_lead(other, {"linkedin_url": "https://www.linkedin.com/in/someone-else"})
    assert repo.get_message(msg.id).auto_hold is True  # approve it yourself for the new person


def test_approved_via_records_auto_approve_only(company):
    turn_on(company)
    auto = draft(person(company), hours_ago=3)
    repo.auto_approve_due(company.id)
    assert repo.get_message(auto.id).approved_via == "auto"
    sent = repo.update_message(auto.id, status="sent")
    assert (sent.status, sent.approved_via) == ("sent", "auto")  # it went out without a person approving it

    again = draft(person(company), hours_ago=3)
    repo.auto_approve_due(company.id)
    assert repo.update_message(again.id, status="approved").approved_via == ""  # a person approved it after all

    bulk = draft(person(company))
    assert repo.bulk_update_drafts(company.id, [(bulk.id, repo.message_version(bulk))], "approve")["done"]
    assert repo.get_message(bulk.id).approved_via == ""
    assert repo.create_message(person(company), NOTE, status="approved").approved_via == ""


# --------------------------------------------------------------------------------------
# What auto-approve leaves alone
# --------------------------------------------------------------------------------------


def test_one_message_per_lead_at_a_time(company):
    turn_on(company)
    lead_id = person(company)
    triplets = [draft(lead_id, hours_ago=5) for _ in range(3)]  # three identical drafts to one person
    result = repo.auto_approve_due(company.id)
    assert result["approved"] == [triplets[0].id] and result["blocked"] == 2  # the oldest only
    assert [status(m.id) for m in triplets] == ["approved", "draft", "draft"]
    assert "another message to this lead is approved and not sent yet" in state(company, triplets[1].id)["reason"]
    assert repo.auto_approve_due(company.id)["approved"] == []

    repo.update_message(triplets[0].id, status="sent")
    assert repo.auto_approve_due(company.id)["approved"] == [triplets[1].id]  # the next one, once the first went

    # A newer draft never overtakes an older one, even when the older one isn't due yet (or is held).
    other = person(company)
    first, second = draft(other, hours_ago=6), draft(other, channel="linkedin_dm", step=2, hours_ago=5)
    repo.update_message(first.id, body="Hi, happy to connect!")  # edited just now: its window starts again
    assert repo.auto_approve_due(company.id)["approved"] == []
    assert state(company, second.id)["reason"] == repo.AUTO_ONE_PER_LEAD_OLDER
    repo.set_auto_hold(first.id, True)
    assert repo.auto_approve_due(company.id)["approved"] == []

    # A message a person approved and nobody sent yet keeps the lead's drafts waiting too.
    busy = person(company)
    repo.create_message(busy, NOTE, status="approved")
    waiting = draft(busy, channel="linkedin_dm", step=2, hours_ago=5)
    assert repo.auto_approve_due(company.id)["approved"] == []
    assert state(company, waiting.id)["reason"] == repo.AUTO_ONE_PER_LEAD_APPROVED


def test_leads_a_person_handles_or_must_never_contact_are_never_auto_approved(company):
    turn_on(company)
    repo.update_company(company.id, {"icp": {"exclude_companies": ["Northwind"]}})
    blocked = draft(person(company, lead_company="Northwind Holdings"), hours_ago=5)
    student = draft(person(company, title="MBA student"), hours_ago=5)
    closed = {}
    for lead_status in ("replied", "meeting", "won", "lost", "disqualified"):
        lead_id = person(company)
        repo.update_lead(lead_id, {"status": lead_status})
        closed[lead_status] = draft(lead_id, hours_ago=5)
    answered = person(company)
    repo.log_reply(answered, "Thanks, send me your rates.")
    repo.update_lead(answered, {"status": "contacted"})  # moved back by hand: the reply is still on record
    after_reply = draft(answered, channel="linkedin_dm", step=2, hours_ago=5)
    fine = draft(person(company), hours_ago=5)

    result = repo.auto_approve_due(company.id)
    assert result["approved"] == [fine.id] and result["blocked"] == 8
    assert "never-contact" in state(company, blocked.id)["reason"]
    assert "student" in state(company, student.id)["reason"]
    for lead_status, msg in closed.items():
        assert f"'{lead_status}'" in state(company, msg.id)["reason"] and status(msg.id) == "draft"
    assert state(company, after_reply.id)["reason"] == repo.LEAD_REPLIED


def test_notes_too_long_for_the_account_and_banned_words_are_never_auto_approved(company):
    turn_on(company, banned_words=["synergy", "game changer"])
    long_note = draft(person(company), body="x" * 201, hours_ago=5)
    banned = draft(person(company), body="Hi, I see real synergy here. Happy to connect!", hours_ago=5)
    subject = draft(person(company, email="a@example.com"), body="Hello there", channel="email",
                    subject="A game changer for your travel", hours_ago=5)
    long_dm = draft(person(company), body="y" * 900, channel="linkedin_dm", hours_ago=5)
    word_inside = draft(person(company), body="Hi, synergyx is not a banned word.", hours_ago=5)

    result = repo.auto_approve_due(company.id)
    assert sorted(result["approved"]) == sorted([long_dm.id, word_inside.id])
    assert "201 characters, more than the 200 your free LinkedIn account allows" in state(company, long_note.id)[
        "reason"]
    assert state(company, banned.id)["reason"] == "it uses a banned word or phrase (synergy)"
    assert "game changer" in state(company, subject.id)["reason"]
    # On Premium the same note fits (300 characters).
    repo.update_company(company.id, {"outreach": {"linkedin_account": "premium"}})
    assert repo.auto_approve_due(company.id)["approved"] == [long_note.id]


def test_nothing_is_approved_while_the_company_is_paused(company):
    turn_on(company)
    msg = draft(person(company), hours_ago=5)
    repo.update_company(company.id, {"status": "paused"})
    assert repo.auto_approve_due(company.id) == {"approved": [], "waiting": 0, "held": 0, "blocked": 0,
                                                 "next_at": None}
    assert state(company, msg.id) == {"state": "blocked", "at": None, "reason": repo.AUTO_COMPANY_PAUSED}
    repo.update_company(company.id, {"status": "active"})
    assert repo.auto_approve_due(company.id)["approved"] == [msg.id]


def test_public_registrations_wait_for_review_with_auto_approve_off(company):
    from openberry.web.pages import as_pending_review

    visitor = CompanyIn(name="Visitor Co", outreach={"auto_approve": True, "auto_approve_hours": 1,
                                                     "auto_approve_since": "2026-01-01T00:00:00Z"})
    pending = repo.create_company(as_pending_review(visitor))
    out = pending.outreach
    assert (pending.status, out.auto_approve, out.auto_approve_hours, out.auto_approve_since) == (
        "paused", False, 2, None)
    msg = draft(person(pending), hours_ago=24 * 30)
    assert repo.auto_approve_due(pending.id)["approved"] == [] and status(msg.id) == "draft"


@pytest.mark.parametrize("change", ["edit", "hold", "skip"])
def test_an_edit_hold_or_skip_at_the_same_moment_wins(company, monkeypatch, change):
    turn_on(company)
    msg = draft(person(company), hours_ago=5)
    real_plan, calls = repo._auto_approve_plan, []

    def plan_then_change(c, company, now, lead_id=None):
        plan = real_plan(c, company, now, lead_id)
        calls.append(1)
        if len(calls) == 2:  # the plan read under the write lock: the user's change lands right after it
            if change == "edit":
                c.execute("UPDATE messages SET body = 'Edited at the same moment', updated_at = ? WHERE id = ?",
                          (repo.iso(now + timedelta(seconds=1)), msg.id))
            elif change == "hold":  # a hold keeps updated_at: the compare-and-set checks auto_hold too
                c.execute("UPDATE messages SET auto_hold = 1 WHERE id = ?", (msg.id,))
            else:
                c.execute("UPDATE messages SET status = 'skipped' WHERE id = ?", (msg.id,))
        return plan

    monkeypatch.setattr(repo, "_auto_approve_plan", plan_then_change)
    assert repo.auto_approve_due(company.id)["approved"] == [] and len(calls) == 2
    after = repo.get_message(msg.id)
    assert after.status == ("skipped" if change == "skip" else "draft") and after.approved_via == ""


def test_describing_drafts_costs_the_same_few_queries_for_any_number(company):
    turn_on(company)

    def statements(more: int) -> tuple[int, int]:
        for _ in range(more):
            draft(person(company))
        messages = repo.list_messages(company.id, status="draft", limit=500)
        seen: list[str] = []
        with db.connect() as c:
            c.set_trace_callback(seen.append)
            states = repo.auto_approve_states(repo.get_company(company.id), messages, conn=c)
        return len(states), len(seen)

    few, many = statements(3), statements(30)
    assert (few[0], many[0]) == (3, 33) and few[1] == many[1] <= 5


# --------------------------------------------------------------------------------------
# When it runs: the scheduler, scans
# --------------------------------------------------------------------------------------


def due_draft_company(name: str, **fields: Any) -> tuple[Company, Message]:
    company = repo.create_company(CompanyIn(name=name, **fields))
    turn_on(company)
    return company, draft(person(company), hours_ago=5)


async def test_the_scheduler_approves_due_drafts_of_every_active_company(monkeypatch):
    first, first_msg = due_draft_company("First Co")
    broken, broken_msg = due_draft_company("Broken Co")
    second, second_msg = due_draft_company("Second Co")
    paused, paused_msg = due_draft_company("Paused Co")
    repo.update_company(paused.id, {"status": "paused"})
    off = repo.create_company(CompanyIn(name="Off Co"))
    off_msg = draft(person(off), hours_ago=5)

    real = repo.auto_approve_due

    def failing(company_id: int, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if company_id == broken.id:
            raise sqlite3.OperationalError("database is locked")
        return real(company_id, *args, **kwargs)

    monkeypatch.setattr(repo, "auto_approve_due", failing)
    # One company's error never stops the others.
    assert await services.auto_approve_active_companies() == {first.id: [first_msg.id], second.id: [second_msg.id]}
    assert [status(m.id) for m in (broken_msg, paused_msg, off_msg)] == ["draft"] * 3

    # The scheduler's tick runs it, with scans and alerts.
    monkeypatch.setattr(repo, "auto_approve_due", real)
    late = draft(person(first), hours_ago=5)

    async def no_scans() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(scheduler, "scan_due_companies", no_scans)
    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.scheduler_loop(stop))
    for _ in range(300):
        if status(late.id) == "approved":
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert status(late.id) == "approved" and status(broken_msg.id) == "approved"


async def test_a_scan_ends_by_approving_due_drafts(company, monkeypatch):
    turn_on(company)
    msg = draft(person(company), hours_ago=5)
    monkeypatch.setattr(services, "get_collectors", lambda names=None: [])
    stats = await services.run_scan(company.id)
    assert stats["auto_approved"] == [msg.id] and status(msg.id) == "approved"
    assert repo.get_scan_run(stats["run_id"]).stats["auto_approved"] == [msg.id]

    # A failure there never fails the scan.
    other = draft(person(company), hours_ago=5)

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(repo, "auto_approve_due", broken)
    stats = await services.run_scan(company.id)
    assert stats["auto_approved"] == [] and stats["status"] == "nothing_configured" and status(other.id) == "draft"


# --------------------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------------------


def schema_without(*columns: str) -> str:
    """db.SCHEMA as an older version wrote it: without these columns (and their comments)."""
    lines = [line for line in db.SCHEMA.split("\n") if not line.strip().startswith(columns)]
    return re.sub(r",([ \t]*(?:--[^\n]*)?)\n\);", r"\1\n);", "\n".join(lines))


def test_a_version_3_database_gets_the_auto_approve_columns_even_when_opened_twice_at_once(settings):
    v3 = schema_without("auto_hold", "approved_via")
    assert "auto_hold" not in v3 and "sent_via" in v3
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(v3)
    now = repo.iso()
    outreach = json.dumps({"sender_name": "Sam", "agent_sending": True})  # a version 3 profile
    conn.execute("INSERT INTO companies (name, outreach, created_at, updated_at) VALUES ('Old Co', ?, ?, ?)",
                 (outreach, now, now))
    conn.execute("INSERT INTO leads (company_id, full_name, linkedin_url, created_at, updated_at) "
                 "VALUES (1, 'Old Lead', 'https://www.linkedin.com/in/old-lead', ?, ?)", (now, now))
    conn.executemany("INSERT INTO messages (company_id, lead_id, channel, body, status, created_at, updated_at, "
                     "sent_at, sent_via) VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?)",
                     [("linkedin_connect", "Sent by the agent", "sent", now, now, now, "agent"),
                      ("linkedin_dm", "Approved by hand", "approved", now, now, None, ""),
                      ("linkedin_dm", "A draft", "draft", now, now, None, "")])
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()

    # Two processes starting at once: both open it, the columns are added once.
    barrier, errors = threading.Barrier(2), []

    def open_it() -> None:
        try:
            barrier.wait()
            db.init_db(settings.db_path)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    with db.connect() as c:
        columns = [r[1] for r in c.execute("PRAGMA table_info(messages)")]
        assert columns.count("auto_hold") == 1 and columns.count("approved_via") == 1
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 4
    messages = sorted(repo.list_messages(1), key=lambda m: m.id)
    assert [(m.status, m.sent_via, m.auto_hold, m.approved_via) for m in messages] == [
        ("sent", "agent", False, ""), ("approved", "", False, ""), ("draft", "", False, "")]
    company = repo.get_company(1)
    assert company.outreach.auto_approve is False and company.outreach.agent_sending is True  # off until turned on
    assert repo.auto_approve_due(1)["approved"] == []
    db.reset_init_cache()
    db.init_db(settings.db_path)  # opening it again changes nothing
    assert repo.get_message(3).status == "draft"


# --------------------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def mcp_client() -> AsyncIterator[Client]:
    async with Client(build_server()) as client:
        yield client


async def ok(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def error_text(client: Client, tool: str, **args: Any) -> str:
    result = await client.call_tool(tool, args)
    assert result.is_error, f"{tool} unexpectedly succeeded: {result.structured_content}"
    return result.content[0].text


async def test_get_send_queue_approves_due_drafts_first(company):
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    msg = draft(person(company), hours_ago=5)
    async with mcp_client() as c:
        queue = await ok(c, "get_send_queue", company_id=company.id)
        assert queue["items"] == [] and queue["auto_approved_now"] == []  # off: a draft is never sent
        turn_on(company)
        queue = await ok(c, "get_send_queue", company_id=company.id)
    assert queue["auto_approved_now"] == [msg.id]
    assert [(i["message_id"], i["auto_approved"]) for i in queue["items"]] == [(msg.id, True)]
    by_hand = repo.create_message(person(company), NOTE, status="approved")
    async with mcp_client() as c:
        queue = await ok(c, "get_send_queue", company_id=company.id)
        doc = " ".join({t.name: t for t in (await c.list_tools()).tools}["get_send_queue"].description.split())
    assert {i["message_id"]: i["auto_approved"] for i in queue["items"]} == {msg.id: True, by_hand.id: False}
    assert "drafts whose review window has passed are approved first" in doc


async def test_claude_sees_when_each_draft_is_approved_automatically(company):
    lead_id = person(company)
    waiting = draft(lead_id, hours_ago=1)
    async with mcp_client() as c:
        rows = (await ok(c, "list_outreach", company_id=company.id))["messages"]
        assert rows[0]["auto_approved"] is False and "auto_approves_at" not in rows[0]  # off
        assert "auto_approve" not in rows[0]
        turn_on(company)
        held = draft(person(company), hours_ago=1)
        repo.set_auto_hold(held.id, True)
        banned = draft(person(company), body="Pure synergy. Happy to connect!")
        repo.update_company(company.id, {"outreach": {"banned_words": ["synergy"]}})
        done = draft(person(company), hours_ago=5)
        repo.auto_approve_due(company.id)
        rows = {r["id"]: r for r in (await ok(c, "list_outreach", company_id=company.id))["messages"]}
        lead = await ok(c, "get_lead", lead_id=lead_id)
    at = repo.get_message(waiting.id).updated_at + 2 * HOUR
    assert rows[waiting.id]["auto_approves_at"] == at.isoformat()
    assert rows[held.id]["auto_approve"] == "held"
    assert rows[banned.id]["auto_approve"] == "it uses a banned word or phrase (synergy)"
    assert rows[done.id]["auto_approved"] is True and rows[done.id]["status"] == "approved"
    assert "auto_approves_at" not in rows[done.id] and "auto_approve" not in rows[done.id]
    assert lead["messages"][0]["auto_approves_at"] == at.isoformat()


async def test_claude_may_hold_a_draft_but_never_release_one(company):
    turn_on(company)
    msg = draft(person(company), hours_ago=1)
    async with mcp_client() as c:
        held = await ok(c, "update_message", message_id=msg.id, auto_hold=True)
        assert held["message"]["auto_hold"] is True and repo.get_message(msg.id).auto_hold
        assert "only the user can let a held draft" in await error_text(c, "update_message", message_id=msg.id,
                                                                          auto_hold=False)
        assert repo.get_message(msg.id).auto_hold
        approved = repo.create_message(person(company), NOTE, status="approved")
        text = await error_text(c, "update_message", message_id=approved.id, auto_hold=True)
        assert "only drafts can be held" in text and repo.get_message(approved.id).status == "approved"
        # status="draft" on an approved message holds it; editing its text doesn't.
        await ok(c, "update_message", message_id=approved.id, status="draft")
        assert repo.get_message(approved.id).auto_hold is True
        other = repo.create_message(person(company), NOTE, status="approved")
        edited = await ok(c, "update_message", message_id=other.id, body="Hi, happy to connect!")
        assert edited["message"]["status"] == "draft" and edited["message"]["auto_hold"] is False
        # Both in one call: the edit is saved and the draft held.
        both = await ok(c, "update_message", message_id=other.id, body="Hi there, happy to connect!", auto_hold=True)
        assert both["message"]["auto_hold"] is True and repo.get_message(other.id).body == "Hi there, happy to connect!"
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert "auto_hold" in tools["update_message"].input_schema["properties"]


async def test_claude_can_turn_auto_approve_off_or_lengthen_it_but_never_the_reverse(company):
    async with mcp_client() as c:
        for change, words in (({"auto_approve": True}, "turn auto-approve on"),
                              ({"auto_approve_since": "2026-01-01T00:00:00Z"}, "change when auto-approve")):
            text = await error_text(c, "update_company", company_id=company.id, changes={"outreach": change})
            assert words in text and "only the user can" in text
        assert repo.get_company(company.id).outreach.auto_approve is False
        turn_on(company, hours=6)
        text = await error_text(c, "update_company", company_id=company.id,
                                changes={"outreach": {"auto_approve_hours": 5}})
        assert "shorten the auto-approve window" in text
        # Its own profile sent back unchanged is fine (the stored since included).
        profile = (await ok(c, "get_company_profile", company_id=company.id))["profile"]
        assert {"auto_approve", "auto_approve_hours", "auto_approve_since"} <= set(profile["outreach"])
        assert profile["outreach"]["auto_approve"] is True and profile["outreach"]["auto_approve_hours"] == 6
        await ok(c, "update_company", company_id=company.id, changes={"outreach": profile["outreach"]})
        await ok(c, "update_company", company_id=company.id, changes={"outreach": {"auto_approve_hours": 24}})
        assert repo.get_company(company.id).outreach.auto_approve_hours == 24
        await ok(c, "update_company", company_id=company.id, changes={"outreach": {"auto_approve": False}})
        out = repo.get_company(company.id).outreach
        assert (out.auto_approve, out.auto_approve_hours) == (False, 24)
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "make its window longer" in " ".join(tools["update_company"].description.split())


async def test_register_company_always_starts_with_auto_approve_off():
    async with mcp_client() as c:
        text = await error_text(c, "register_company", name="Eager Co", profile={"name": "Eager Co", "outreach": {
            "auto_approve": True}})
        assert "turn auto-approve on" in text and not any(co.name == "Eager Co" for co in repo.list_companies())
        created = await ok(c, "register_company", name="Calm Co", profile={"name": "Calm Co", "outreach": {
            "auto_approve_hours": 12}})
    out = repo.get_company(created["company_id"]).outreach
    assert (out.auto_approve, out.auto_approve_hours, out.auto_approve_since) == (False, 12, None)


async def test_saving_a_draft_tells_claude_when_it_is_approved_automatically(company):
    lead_id = person(company)
    async with mcp_client() as c:
        saved = await ok(c, "save_outreach_message", lead_id=lead_id, body=NOTE, channel="linkedin_connect")
        assert "auto_approves_at" not in saved and "Saved as a draft only" in saved["reminder"]
        turn_on(company)
        saved = await ok(c, "save_outreach_message", lead_id=lead_id, body=NOTE + " Thanks!",
                         channel="linkedin_connect")
        server = build_server()
    msg = repo.get_message(saved["message_id"])
    assert saved["auto_approves_at"] == (msg.updated_at + 2 * HOUR).isoformat()
    assert "Auto-approve is on for this company" in saved["reminder"]
    flat = " ".join(server.instructions.split())
    assert "auto-approve" in flat and "auto_hold=true" in flat
