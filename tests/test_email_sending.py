"""Email sending from the user's own mailbox: the rules in repo.py, the send loop in services.py and the inbox check.

Off by default; approved emails only, exactly as approved plus the footer; the limits, the minutes between two
emails and the sending hours; one email per lead at a time; never twice (claim, DATA, record); pauses; replies,
opt-outs and bounces from the inbox. Every test uses the fake mail servers of email_fakes.py: no network.
"""

from __future__ import annotations

import dataclasses
import email
import email.policy
import json
import smtplib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from email_fakes import FROM, OUR_ID, connect_mailbox, eml
from openberry import db, mailer, repo, services
from openberry.models import Company, CompanyIn, LeadIn, Message, OutreachConfig

DUBAI = timezone(timedelta(hours=4))
NOW = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)  # Monday 10:00 in Dubai: inside the default sending hours
FOOTER = ('Acme Chauffeurs · Office 1204, Bay Square, Business Bay, Dubai\n'
          'Not interested? Reply "unsubscribe" and we won\'t email you again.')
BODY = "Hi Lina,\n\nI saw Gulf Bank is opening a Dubai office. We drive executives to and from DXB.\n\nSam"


@pytest.fixture(autouse=True)
def dubai_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sending hours are on the computer's clock: the tests' computer is in Dubai."""
    monkeypatch.setattr(repo, "EMAIL_LOCAL_TZ", DUBAI)
    monkeypatch.setattr(services, "_sending_stopped", "")


@pytest.fixture
def box(settings, offline_mail):
    """A connected mailbox whose inbox was just checked."""
    connect_mailbox(settings)
    repo.save_inbox_check(FROM, True, now=NOW)
    return offline_mail


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

_people = iter(range(1, 10_000))


def turn_on(company: Company, **outreach: Any) -> Company:
    """What the Email sending card does when the user turns it on."""
    return repo.update_company(company.id, {"outreach": {"email_sending": True, "email_footer": FOOTER, **outreach}})


def lead(company: Company, email_address: str | None = None, **fields: Any) -> int:
    n = next(_people)
    data = {"full_name": f"Person {n}", "title": "Travel Manager", "lead_company": f"Client {n}",
            "email": f"person{n}@client{n}.example" if email_address is None else email_address, **fields}
    found, _ = repo.upsert_lead(company.id, LeadIn(**data))
    return found.id


def approved(lead_id: int, subject: str = "Airport transfers for your Dubai office", body: str = BODY,
             step: int = 1, status: str = "approved", channel: str = "email") -> Message:
    return repo.create_message(lead_id, body, channel=channel, subject=subject, step=step, status=status,
                               generated_by="claude")


def queued(company: Company, now: datetime = NOW) -> list[int]:
    queue = repo.email_queue(company.id, now=now)
    return [item["message_id"] for item in queue["items"]] if not queue["blocked_reason"] else []


def skip_reason(company: Company, message_id: int, now: datetime = NOW) -> str:
    return {s["message_id"]: s["reason"] for s in repo.email_queue(company.id, now=now)["skipped"]}[message_id]


def send(now: datetime = NOW, fresh: bool = True) -> dict[str, Any]:
    """One turn of the email loop's sending step (after a good inbox check, unless fresh=False)."""
    if fresh:
        repo.save_inbox_check(FROM, True, now=now)
    return services.send_next_email(now=now)


def sent_data(world) -> list[email.message.EmailMessage]:
    return [email.message_from_bytes(s["data"], policy=email.policy.default) for s in world.sent]


def row(message_id: int) -> sqlite3.Row:
    with db.connect() as c:
        return c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()


def set_row(message_id: int, **fields: Any) -> None:
    with db.connect() as c:
        c.execute(f"UPDATE messages SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                  [*fields.values(), message_id])


def sent_from_mailbox(lead_id: int, header_id: str = OUR_ID, when: datetime = NOW, step: int = 1) -> Message:
    """An email OpenBerry sent earlier (as record_email_sent leaves it)."""
    msg = approved(lead_id, step=step)
    set_row(msg.id, status="sent", sent_at=repo.iso(when), sent_via="smtp", email_state="sent",
            email_message_id=header_id, email_to=repo.get_lead(lead_id).email, email_claimed_at=repo.iso(when))
    repo.update_lead(lead_id, {"status": "contacted"})
    return repo.get_message(msg.id)


# --------------------------------------------------------------------------------------
# Settings: off by default, kept by profile saves
# --------------------------------------------------------------------------------------


def test_settings_defaults_and_bounds():
    cfg = OutreachConfig()
    assert cfg.email_sending is False and cfg.email_send_auto_approved is False
    assert (cfg.email_daily_limit, cfg.email_gap_minutes) == (30, 5)
    assert (cfg.email_days, cfg.email_start_hour, cfg.email_end_hour) == ([0, 1, 2, 3, 4], 9, 18)
    assert cfg.email_footer == "" and cfg.email_paused_until is None
    for bad in ({"email_daily_limit": 0}, {"email_daily_limit": 101}, {"email_gap_minutes": 0},
                {"email_gap_minutes": 4}, {"email_start_hour": 18, "email_end_hour": 9}, {"email_days": ["Funday"]},
                {"email_footer": "x" * 601}):
        with pytest.raises(ValueError):
            OutreachConfig(**bad)
    assert OutreachConfig(email_days="Sun, mon, 2").email_days == [0, 2, 6]
    assert OutreachConfig(email_footer="a\r\nb  ").email_footer == "a\nb"


def test_off_by_default_nothing_is_sent(company, box):
    message = approved(lead(company))
    queue = repo.email_queue(company.id, now=NOW)
    assert queue["enabled"] is False and queue["blocked_reason"] == "disabled" and queued(company) == []
    assert queue["eligible_total"] == 1  # what would go once the user turns it on (the card says how many)
    assert "Only the user can turn it on" in repo.email_blocked_message(queue)
    assert send() == {"status": "idle"}
    assert box.smtp_commands() == [] and repo.get_message(message.id).status == "approved"


def test_a_whole_profile_save_keeps_every_email_setting_and_the_pause(company, box):
    turn_on(company, email_daily_limit=12, email_gap_minutes=9, email_days=[6, 0], email_start_hour=8,
            email_end_hour=20, email_send_auto_approved=True)
    with db.connect() as c:
        repo._save_email_pause(c, company.id, NOW + timedelta(hours=24), "3 hard bounces in 24 hours")
    profile = CompanyIn.model_validate(repo.get_company(company.id).model_dump(mode="json"))
    profile.outreach = profile.outreach.model_copy(update={"email_sending": False, "email_footer": "",
                                                            "email_paused_until": None, "email_daily_limit": 100})
    saved = repo.update_company(company.id, profile).outreach  # the profile form neither shows nor changes them
    assert saved.email_sending is True and saved.email_footer == FOOTER and saved.email_daily_limit == 12
    assert (saved.email_gap_minutes, saved.email_days, saved.email_start_hour, saved.email_end_hour) == (9, [0, 6], 8,
                                                                                                         20)
    assert saved.email_send_auto_approved is True and saved.email_paused_until == NOW + timedelta(hours=24)
    # A partial save (the Email sending card) changes them; the pause survives it too.
    out = repo.update_company(company.id, {"outreach": {"email_sending": False, "email_paused_until": None}}).outreach
    assert out.email_sending is False and out.email_paused_until == NOW + timedelta(hours=24)
    assert repo.resume_email_sending(company.id).outreach.email_paused_until is None


# --------------------------------------------------------------------------------------
# What blocks sending for the whole company
# --------------------------------------------------------------------------------------


def test_nothing_goes_out_without_a_mailbox_and_a_fresh_inbox_check(company, settings, offline_mail):
    turn_on(company)
    approved(lead(company))
    assert repo.email_queue(company.id, now=NOW)["blocked_reason"] == "not_connected"
    assert services.send_next_email(now=NOW) == {"status": "not_connected"}
    connect_mailbox(settings)
    assert repo.email_queue(company.id, now=NOW)["blocked_reason"] == "inbox_unchecked"  # never checked
    repo.save_inbox_check(FROM, True, now=NOW - timedelta(minutes=31))
    assert repo.email_queue(company.id, now=NOW)["blocked_reason"] == "inbox_unchecked"  # too long ago
    assert send(fresh=False) == {"status": "idle"}
    repo.save_inbox_check(FROM, False, "Your mailbox refused the username or password", now=NOW)
    state = repo.email_queue(company.id, now=NOW)
    assert state["blocked_reason"] == "inbox_unchecked" and "refused the username" in repo.email_blocked_message(state)
    repo.save_inbox_check(FROM, True, now=NOW - timedelta(minutes=29))
    assert repo.email_queue(company.id, now=NOW)["blocked_reason"] == ""
    assert offline_mail.smtp_commands() == []


def test_blocked_reasons_in_order(company, box):
    turn_on(company)
    approved(lead(company))
    repo.update_company(company.id, {"outreach": {"email_footer": "Acme, [your business address]"}})
    state = repo.email_queue(company.id, now=NOW)
    assert state["blocked_reason"] == "footer"
    assert "replace [your business address]" in repo.email_blocked_message(state)
    turn_on(company)
    repo.update_company(company.id, {"status": "paused"})
    assert repo.email_queue(company.id, now=NOW)["blocked_reason"] == "company_paused"
    repo.update_company(company.id, {"status": "active"})
    with db.connect() as c:
        repo._save_email_pause(c, company.id, NOW + timedelta(hours=2), "2 spam rejections")
    state = repo.email_queue(company.id, now=NOW)
    assert state["blocked_reason"] == "paused" and "Only the user can resume it" in repo.email_blocked_message(state)
    repo.save_inbox_check(FROM, True, now=NOW + timedelta(hours=3))
    assert repo.email_queue(company.id, now=NOW + timedelta(hours=3))["blocked_reason"] == ""  # the pause ran out
    repo.resume_email_sending(company.id)
    saturday = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
    repo.save_inbox_check(FROM, True, now=saturday)
    state = repo.email_queue(company.id, now=saturday)
    assert state["blocked_reason"] == "outside_hours" and state["items"]  # ready, waiting for Monday
    assert state["next_send_at"] == "2026-10-19T05:00:00+00:00"  # Monday 09:00 in Dubai


def test_sending_hours_are_on_the_computers_clock():
    cfg = OutreachConfig()  # Monday to Friday, 09:00 to 18:00
    at = lambda day, hour, minute=0: datetime(2026, 10, day, hour, minute, tzinfo=DUBAI)  # noqa: E731
    assert repo.in_sending_hours(cfg, at(12, 9)) and repo.in_sending_hours(cfg, at(16, 17, 59))
    assert not repo.in_sending_hours(cfg, at(12, 8, 59)) and not repo.in_sending_hours(cfg, at(16, 18))
    assert not repo.in_sending_hours(cfg, at(17, 12))  # Saturday
    assert repo.next_sending_time(cfg, at(16, 18, 30)) == at(19, 9)
    assert repo.next_sending_time(OutreachConfig(email_days=[]), at(12, 10)) is None
    late = OutreachConfig(email_days=[0, 1, 2, 3, 4, 5, 6], email_start_hour=0, email_end_hour=24)
    assert repo.in_sending_hours(late, at(17, 23, 59))


# --------------------------------------------------------------------------------------
# Which emails: approved only, exactly as approved, to the right people
# --------------------------------------------------------------------------------------


def test_only_approved_emails_go_out_exactly_as_approved_with_the_footer(company, box):
    turn_on(company)
    lina = lead(company, "lina@gulfbank.example", full_name="Lina Noor")
    message = approved(lina, subject="Airport transfers – Dubaï office")
    draft = approved(lead(company), status="draft")
    skipped = approved(lead(company), status="skipped")
    linkedin = approved(lead(company, linkedin_url="https://www.linkedin.com/in/x-1"), channel="linkedin_dm")
    assert queued(company) == [message.id]
    result = send()
    assert result == {"status": "sent", "message_id": message.id}
    [out] = sent_data(box)
    assert out["Subject"] == "Airport transfers – Dubaï office"
    assert out["To"].addresses[0].addr_spec == "lina@gulfbank.example"
    assert out["To"].addresses[0].display_name == repo.get_lead(lina).full_name
    assert out.get_content().replace("\r\n", "\n") == f"{BODY}\n\n{FOOTER}\n"
    assert out["From"] == "Sam Rahman <sam@acme-mail.example>" and out["Message-ID"] == row(message.id)[
        "email_message_id"]
    stored = repo.get_message(message.id)
    assert (stored.status, stored.sent_via, stored.email_state, stored.email_to) == ("sent", "smtp", "sent",
                                                                                    "lina@gulfbank.example")
    assert stored.sent_at == NOW and stored.body == BODY  # the footer is added to the email, never to the message
    assert repo.get_lead(lina).status == "contacted"
    for other in (draft, skipped, linkedin):
        assert repo.get_message(other.id).sent_at is None
    assert box.appended and box.appended[0][0] == "Sent"  # a copy in the Sent folder
    assert send(NOW + timedelta(minutes=10)) == {"status": "idle"}


def test_why_approved_emails_are_held_back(company, box):
    turn_on(company, email_send_auto_approved=False)
    other = repo.create_company(CompanyIn(name="Other Co"))
    reasons = {}

    def held(expected: str, message: Message) -> None:
        reasons[message.id] = expected

    held("it has no subject", approved(lead(company), subject=""))
    lost = lead(company)
    repo.update_lead(lost, {"status": "lost"})
    held("the lead is marked Lost", approved(lost))
    replied = lead(company)
    repo.create_message(replied, "Thanks!", channel="email", status="received", direction="inbound")
    held(repo.LEAD_REPLIED, approved(replied))
    repo.update_company(company.id, {"icp": {"exclude_companies": ["Blocked Hotel", "rival-limos.example"]}})
    held("never-contact list", approved(lead(company, lead_company="Blocked Hotel")))
    held("domain is on your never-contact list (rival-limos.example)",
         approved(lead(company, "ops@mail.rival-limos.example")))
    held("the lead has no valid email address", approved(lead(company, "")))
    held("the lead has no valid email address", approved(lead(company, "not-an-address")))
    held("no-reply or system address", approved(lead(company, "noreply@hotel.example")))
    elsewhere = repo.upsert_lead(other.id, LeadIn(full_name="Same Person", email="Opted@Out.example"))[0]
    repo.record_opt_out(elsewhere.id, "email reply", "unsubscribe")  # in another company
    held("opted@out.example asked not to be emailed", approved(lead(company, "opted@out.example")))
    repo.add_suppression("dead@bounced.example", "bounce", "5.1.1 user unknown")
    held("dead@bounced.example bounced on", approved(lead(company, "dead@bounced.example")))
    two = lead(company)
    first = approved(two)
    held(repo.EMAIL_ONE_AT_A_TIME, approved(two, subject="Another", step=1))
    again = lead(company)
    sent_from_mailbox(again, "<earlier-1@acme-mail.example>", NOW - timedelta(days=30))
    held("step 1 was already emailed to this lead", approved(again))
    linkedin_first = lead(company, linkedin_url="https://www.linkedin.com/in/li-1")
    li = approved(linkedin_first, channel="linkedin_dm")
    repo.update_message(li.id, status="sent")
    set_row(li.id, sent_at=repo.iso(NOW - timedelta(days=1)))
    held("not due before", approved(linkedin_first, step=2))  # a LinkedIn message yesterday delays the email
    auto = approved(lead(company))
    set_row(auto.id, approved_via="auto")
    held("auto-approve approved it", auto)
    account = repo.upsert_lead(company.id, LeadIn(lead_company="Palm Hotel", email="reservations@palmhotel.example"))[0]
    for_account = approved(account.id)

    queue = repo.email_queue(company.id, now=NOW)
    skipped = {s["message_id"]: s["reason"] for s in queue["skipped"]}
    for message_id, expected in reasons.items():
        assert expected in skipped[message_id], (expected, skipped.get(message_id))
    assert [item["message_id"] for item in queue["items"]] == [first.id, for_account.id]  # oldest approved first
    assert queue["items"][1]["lead_name"] == "Palm Hotel"
    # Only the eligible ones are ever claimed, whatever happens.
    sent = [send(NOW + timedelta(minutes=6 * i)) for i in range(4)]
    assert [s.get("message_id") for s in sent] == [first.id, for_account.id, None, None]


def test_auto_approved_emails_need_their_own_switch_and_claudes_approvals_count(company, box):
    turn_on(company)
    auto = approved(lead(company))
    set_row(auto.id, approved_via="auto")
    by_claude = repo.update_message_as(approved(lead(company), status="draft").id, "claude", status="approved")
    assert queued(company) == [by_claude.id]  # approved means approved: the user's word, through Claude
    assert "allow them on the Email sending card" in skip_reason(company, auto.id)
    repo.update_company(company.id, {"outreach": {"email_send_auto_approved": True}})
    assert set(queued(company)) == {auto.id, by_claude.id}
    # Approving it yourself clears approved_via: it then goes without the switch.
    repo.update_company(company.id, {"outreach": {"email_send_auto_approved": False}})
    repo.update_message(auto.id, status="approved")
    assert set(queued(company)) == {auto.id, by_claude.id}


def test_a_new_address_sends_approved_emails_back_to_draft(company, box):
    turn_on(company)
    changed = lead(company, "old@client.example")
    message = approved(changed)
    repo.update_lead(changed, {"email": "new@client.example"})
    stored = repo.get_message(message.id)
    assert (stored.status, stored.auto_hold) == ("draft", True)  # an approval covers the address too
    filled = lead(company, "", linkedin_url="https://www.linkedin.com/in/fill-me")
    second = approved(filled)
    repo.upsert_lead(company.id, LeadIn(full_name="Whoever", linkedin_url="https://www.linkedin.com/in/fill-me",
                                        email="found@client.example"))
    assert repo.get_message(second.id).status == "draft"
    left = lead(company)
    third = approved(left)
    repo.update_lead(left, {"status": "lost"})
    assert repo.get_message(third.id).status == "draft"
    assert queued(company) == []


# --------------------------------------------------------------------------------------
# Limits and pace
# --------------------------------------------------------------------------------------


def test_the_daily_limit_is_rolling_and_counts_emails_in_flight_or_unknown(company, box):
    turn_on(company, email_daily_limit=3)
    messages = [approved(lead(company)) for _ in range(5)]
    assert send()["status"] == "sent"
    set_row(messages[1].id, email_state="unknown", email_claimed_at=repo.iso(NOW + timedelta(minutes=5)))
    set_row(messages[2].id, email_state="data", email_claim="other", email_claimed_at=repo.iso(NOW + timedelta(
        minutes=10)))
    state = repo.email_sending_status(company.id, now=NOW + timedelta(minutes=15))
    assert state["sent_last_24h"] == 3 and state["remaining"] == 0 and state["blocked_reason"] == "daily_limit"
    assert state["limit_frees_at"] == repo.iso(NOW + timedelta(hours=24))
    assert "daily limit of 3" in repo.email_blocked_message(state)
    set_row(messages[2].id, email_state="unknown")
    assert send(NOW + timedelta(hours=23, minutes=59))["status"] == "idle"
    later = NOW + timedelta(hours=24, minutes=6)
    assert send(later)["status"] == "sent"  # the first two left the window
    # Your own Mark sent doesn't count: only what OpenBerry sent (or may have sent).
    counted = repo.email_sending_status(company.id, now=later)["sent_last_24h"]
    repo.update_message(messages[4].id, status="sent")
    assert repo.email_sending_status(company.id, now=later)["sent_last_24h"] == counted == 2


def test_the_mailbox_cap_covers_every_company(company, box, monkeypatch):
    monkeypatch.setattr(repo, "MAILBOX_DAILY_MAX", 2)
    other = turn_on(repo.create_company(CompanyIn(name="Second Co")))
    turn_on(company)
    approved(lead(company))
    approved(lead(other))
    approved(lead(company))
    assert send()["status"] == "sent" and send(NOW + timedelta(minutes=5))["status"] == "sent"
    assert repo.email_sending_status(company.id, now=NOW + timedelta(minutes=10))["blocked_reason"] == "mailbox_limit"
    assert send(NOW + timedelta(minutes=10))["status"] == "idle"
    # The company that waited longest goes first: one each.
    assert len(repo.list_messages(company.id, status="sent")) == len(repo.list_messages(other.id, status="sent")) == 1


def test_minutes_between_two_emails_and_one_in_flight(company, box):
    turn_on(company)  # 5 minutes apart
    first, second = approved(lead(company)), approved(lead(company))
    assert send()["message_id"] == first.id
    assert send(NOW + timedelta(minutes=4, seconds=59))["status"] == "idle"
    state = repo.email_sending_status(company.id, now=NOW + timedelta(minutes=4))
    assert state["next_send_at"] == repo.iso(NOW + timedelta(minutes=5))
    set_row(first.id, email_state="claimed")  # another email in flight (e.g. a second dashboard process)
    assert repo.claim_next_email("x", FROM, now=NOW + timedelta(minutes=6)) is None
    set_row(first.id, email_state="sent")
    assert send(NOW + timedelta(minutes=5))["message_id"] == second.id


def test_a_burst_of_approvals_goes_out_one_by_one(company, box):
    turn_on(company, email_daily_limit=30)
    for _ in range(40):
        approved(lead(company))
    results = [send(NOW + timedelta(minutes=5 * i))["status"] for i in range(45)]
    assert results.count("sent") == 30 and len(box.sent) == 30
    assert all(status == "idle" for status in results[30:])


# --------------------------------------------------------------------------------------
# Never twice: claim, DATA, record
# --------------------------------------------------------------------------------------


def test_two_workers_claiming_at_once_get_one_claim(company, box):
    turn_on(company)
    for _ in range(4):
        approved(lead(company))
    barrier, jobs = threading.Barrier(6), []

    def claim(n: int) -> None:
        barrier.wait()
        jobs.append(repo.claim_next_email(f"worker-{n}", FROM, now=NOW))

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([j for j in jobs if j is not None]) == 1


def test_a_stale_claim_goes_back_to_the_queue_and_fails_after_five_attempts(company, box):
    turn_on(company)
    message = approved(lead(company))
    for attempt in range(1, 6):
        when = NOW + timedelta(minutes=20 * attempt)
        job = repo.claim_next_email(f"crashed-{attempt}", FROM, now=when)
        assert job is not None and job.message_id == message.id
        repo.save_inbox_check(FROM, True, now=when + timedelta(minutes=11))
        repo.expire_email_claims(now=when + timedelta(minutes=11))  # OpenBerry stopped before DATA
        stored = row(message.id)
        assert stored["email_attempts"] == attempt
    assert stored["email_state"] == "failed" and "Tried 5 times" in stored["send_error"]
    assert queued(company, NOW + timedelta(hours=3)) == []


def test_a_crash_during_data_makes_it_unknown_and_it_is_never_sent_again(company, box):
    turn_on(company)
    message = approved(lead(company))
    job = repo.claim_next_email("w1", FROM, now=NOW)
    assert repo.mark_email_data(job.message_id, "w1", now=NOW)
    repo.expire_email_claims(now=NOW + timedelta(minutes=11))
    stored = repo.get_message(message.id)
    assert stored.email_state == "unknown" and "It may have gone out" in stored.send_error
    assert stored.status == "approved" and stored.sent_at is None
    assert [e["kind"] for e in repo.list_email_events(lead_id=message.lead_id)] == ["unknown"]
    for minutes in (12, 60, 600):
        assert send(NOW + timedelta(minutes=minutes))["status"] == "idle"
    assert "may have been sent already" in skip_reason(company, message.id, NOW + timedelta(hours=1))
    assert repo.email_attention(company.id)["messages"][0].id == message.id
    # The user checks their Sent folder: it went out.
    marked = repo.resolve_email(message.id, "mark_sent", now=NOW + timedelta(hours=1))
    assert (marked.status, marked.sent_via, marked.email_state) == ("sent", "smtp", "sent")
    assert marked.sent_at == NOW  # when it was being sent
    with pytest.raises(ValueError):
        repo.resolve_email(message.id, "send_again")


def test_send_again_keeps_the_message_id(company, box):
    turn_on(company)
    message = approved(lead(company))
    box.fail["data"] = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    assert send()["status"] == mailer.UNKNOWN
    header_id = row(message.id)["email_message_id"]
    assert row(message.id)["email_state"] == "unknown"
    repo.resolve_email(message.id, "send_again")
    box.fail.clear()
    repo.retry_mailbox(FROM)  # the failure paused the mailbox for a few minutes
    assert send(NOW + timedelta(minutes=10))["status"] == "sent"
    assert sent_data(box)[0]["Message-ID"] == header_id  # receiving systems may drop the duplicate


def test_a_success_recorded_after_the_claim_expired_still_counts(company, box):
    turn_on(company)
    message = approved(lead(company))
    job = repo.claim_next_email("slow", FROM, now=NOW)
    assert repo.mark_email_data(message.id, "slow", now=NOW)
    repo.expire_email_claims(now=NOW + timedelta(minutes=11))  # unknown meanwhile
    repo.record_email_sent(message.id, job.header_id, FROM, now=NOW + timedelta(minutes=12))
    stored = repo.get_message(message.id)
    assert (stored.status, stored.email_state, stored.sent_via) == ("sent", "sent", "smtp")
    repo.record_email_sent(message.id, job.header_id, FROM, now=NOW + timedelta(minutes=13))  # twice: no change
    assert repo.get_message(message.id).sent_at == NOW + timedelta(minutes=12)


def test_a_sent_email_that_cant_be_recorded_stops_sending_until_a_restart(company, box, monkeypatch):
    turn_on(company)
    first, second = approved(lead(company)), approved(lead(company))
    monkeypatch.setattr(services, "RECORD_WAIT_SECONDS", 0)
    real = repo.record_email_sent

    def broken(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(repo, "record_email_sent", broken)
    result = send()
    assert result["status"] == "stopped" and "Restart OpenBerry" in result["text"]
    assert len(box.sent) == 1 and row(first.id)["email_state"] == "data"
    monkeypatch.setattr(repo, "record_email_sent", real)  # the database works again: still nothing until a restart
    assert send(NOW + timedelta(minutes=10))["status"] == "stopped" and len(box.sent) == 1
    assert services.sending_stopped()
    repo.expire_email_claims(now=NOW + timedelta(minutes=11))
    assert row(first.id)["email_state"] == "unknown" and row(second.id)["email_state"] == ""


def test_a_sent_email_set_back_to_approved_is_never_sent_again(company, box):
    turn_on(company)
    message = approved(lead(company))
    assert send()["status"] == "sent"
    repo.update_message(message.id, status="approved")
    assert "already recorded as sent" in skip_reason(company, message.id, NOW + timedelta(hours=1))
    assert send(NOW + timedelta(hours=1))["status"] == "idle" and len(box.sent) == 1


def test_an_email_in_flight_cant_be_edited_or_deleted_and_a_sent_one_keeps_its_text(company, box):
    turn_on(company)
    message = approved(lead(company))
    repo.claim_next_email("w", FROM, now=NOW)
    for change in ({"body": "New text"}, {"status": "skipped"}, {"subject": "Other"}):
        with pytest.raises(ValueError, match="being sent right now"):
            repo.update_message(message.id, **change)
    with pytest.raises(ValueError, match="being sent right now"):
        repo.delete_message(message.id)
    repo.record_email_failure(message.id, "w", mailer.ABORTED, "")
    assert send(NOW + timedelta(minutes=6))["status"] == "sent"
    with pytest.raises(ValueError, match="can't be changed"):
        repo.update_message(message.id, body="Edited after sending")
    assert repo.update_message(message.id, status="replied").status == "replied"  # status changes still work


def test_a_reply_before_data_stops_the_email_and_one_during_data_is_still_recorded(company, box):
    turn_on(company)
    early, late = lead(company), lead(company)
    early_msg, late_msg = approved(early), approved(late)
    job = repo.claim_next_email("w1", FROM, now=NOW)
    assert job.message_id == early_msg.id
    repo.log_reply(early, "Not now, thanks", channel="email")  # skips it
    assert repo.mark_email_data(job.message_id, "w1", now=NOW) is False  # so DATA is never sent
    assert row(early_msg.id)["email_state"] == "" and repo.get_message(early_msg.id).status == "skipped"
    job = repo.claim_next_email("w2", FROM, now=NOW + timedelta(minutes=6))
    assert repo.mark_email_data(job.message_id, "w2", now=NOW + timedelta(minutes=6))
    repo.log_reply(late, "Thanks, interested", channel="email")  # while the server receives it
    repo.record_email_sent(late_msg.id, job.header_id, FROM, now=NOW + timedelta(minutes=6))
    assert repo.get_message(late_msg.id).status == "sent"  # the truth is recorded


def test_on_data_aborts_when_sending_was_turned_off_meanwhile(company, box, monkeypatch):
    turn_on(company)
    approved(lead(company))
    real = repo.mark_email_data

    def turned_off_first(message_id: int, claim: str, now: Any = None) -> bool:
        repo.update_company(company.id, {"outreach": {"email_sending": False}})
        return real(message_id, claim, now=now)

    monkeypatch.setattr(repo, "mark_email_data", turned_off_first)
    assert send()["status"] == mailer.ABORTED
    assert box.sent == [] and "rset" in box.smtp_commands() and "data" not in box.smtp_commands()


# --------------------------------------------------------------------------------------
# Failures and pauses
# --------------------------------------------------------------------------------------


def test_a_refused_login_pauses_the_mailbox_until_the_user_acts(company, box):
    turn_on(company)
    message = approved(lead(company))
    box.fail["login"] = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    assert send()["status"] == mailer.AUTH
    state = repo.mailbox_state(FROM, now=NOW)
    assert state["paused"] and state["auth_failed"] and "Username and Password not accepted" in state["pause_reason"]
    assert row(message.id)["email_state"] == "" and row(message.id)["email_attempts"] == 0  # back in the queue
    assert repo.email_sending_status(company.id, now=NOW + timedelta(days=2))["blocked_reason"] == "mailbox_paused"
    assert send(NOW + timedelta(days=2))["status"] == "idle"
    box.fail.clear()
    repo.retry_mailbox(FROM, now=NOW + timedelta(days=2))  # Try again, or saving new settings
    assert send(NOW + timedelta(days=2))["status"] == "sent"


def test_temporary_failures_pause_for_longer_each_time(company, box):
    turn_on(company)
    message = approved(lead(company))
    box.fail["connect"] = ConnectionRefusedError(111, "Connection refused")
    pauses = []
    when = NOW
    for _ in range(3):
        assert send(when)["status"] == mailer.RETRY
        until = datetime.fromisoformat(repo.mailbox_state(FROM, now=when)["paused_until"])
        pauses.append(until - when)
        when = until + timedelta(seconds=1)
    assert pauses == [timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60)]
    assert row(message.id)["email_attempts"] == 3 and row(message.id)["email_state"] == ""
    assert "3 attempts failed in a row" in repo.mailbox_state(FROM, now=NOW)["pause_reason"]
    box.fail.clear()
    assert send(when)["status"] == "sent"
    assert repo.mailbox_state(FROM, now=when)["failures"] == 0


def test_a_hard_bounce_at_rcpt_suppresses_the_address_and_three_pause_the_company(company, box):
    turn_on(company)
    messages = [approved(lead(company)) for _ in range(4)]
    box.fail["rcpt"] = (550, b"5.1.1 <x>: Recipient address rejected: User unknown")
    for i in range(3):
        assert send(NOW + timedelta(minutes=5 * i))["status"] == mailer.BOUNCE
    first = repo.get_message(messages[0].id)
    assert first.email_state == "failed" and "doesn't exist" in first.send_error and first.sent_at is None
    assert "email-bounced" in repo.get_lead(first.lead_id).tags
    assert repo.suppressed_addresses([first.email_to])[first.email_to]["reason"] == "bounce"
    out = repo.get_company(company.id).outreach
    assert out.email_paused_until == NOW + timedelta(minutes=10) + repo.EMAIL_PAUSE and "3 hard bounces" in \
        out.email_pause_reason
    assert send(NOW + timedelta(minutes=20))["status"] == "idle"


def test_the_provider_refusing_pauses_the_mailbox_for_a_day(company, box):
    turn_on(company)
    message = approved(lead(company))
    box.fail["data"] = (554, b"5.7.1 Message rejected: suspected spam")
    assert send()["status"] == mailer.PROVIDER_BLOCK
    state = repo.mailbox_state(FROM, now=NOW)
    assert state["paused_until"] == repo.iso(NOW + timedelta(hours=24)) and "suspected spam" in state["pause_reason"]
    assert row(message.id)["email_state"] == "failed"
    repo.resolve_email(message.id, "retry")
    assert row(message.id)["email_state"] == "" and row(message.id)["email_attempts"] == 0


def test_a_broken_connection_during_data_is_unknown(company, box):
    turn_on(company)
    message = approved(lead(company))
    box.fail["data"] = TimeoutError("timed out")
    assert send()["status"] == mailer.UNKNOWN
    assert row(message.id)["email_state"] == "unknown" and repo.get_message(message.id).sent_at is None
    # What to do about the email stays on the email; the mailbox's short wait has its own words.
    assert "Mark sent or Send again" in repo.get_message(message.id).send_error
    assert repo.mailbox_state(FROM, now=NOW)["pause_reason"] == repo.DROPPED_TEXT


def test_two_spam_rejections_in_a_day_pause_the_company(company, box):
    turn_on(company)
    inbound = mailer.classify_inbound(eml("dsn_policy.eml", lead="lina@gulfbank.example", ours=OUR_ID))
    for n in range(2):
        found = lead(company, "lina@gulfbank.example" if n == 0 else f"other{n}@gulfbank.example")
        sent_from_mailbox(found, f"<policy-{n}@acme-mail.example>")
        about_it = dataclasses.replace(inbound, message_id=f"<dsn-{n}@x.example>",
                                       original_ids=(f"<policy-{n}@acme-mail.example>",))
        result = repo.record_inbound(about_it, f"uid:INBOX:1:{n}", now=NOW)
        assert result["kind"] == mailer.POLICY_REJECT
    out = repo.get_company(company.id).outreach
    assert out.email_paused_until == NOW + repo.EMAIL_PAUSE and "2 spam or policy rejections" in out.email_pause_reason
    assert repo.email_sending_status(company.id, now=NOW)["blocked_reason"] == "paused"


# --------------------------------------------------------------------------------------
# The inbox: replies, opt-outs, bounces
# --------------------------------------------------------------------------------------


def lina_sent(company: Company, box_world, **fields: Any) -> tuple[int, Message]:
    """Lina got an email from OpenBerry (Message-ID OUR_ID), and the inbox was checked once since."""
    lina = lead(company, "lina@gulfbank.example", full_name="Lina Noor", **fields)
    message = sent_from_mailbox(lina)
    assert services.check_inbox(now=NOW)["status"] == "ok"  # the first check notes where the inbox is
    return lina, message


def deliver(world, name: str, folder: str = "INBOX", **extra: str) -> None:
    world.deliver(eml(name, lead="lina@gulfbank.example", ours=OUR_ID, **extra), folder)


def test_the_first_check_reads_no_history(company, box):
    turn_on(company)
    lina = lead(company, "lina@gulfbank.example")
    sent_from_mailbox(lina)
    deliver(box, "reply_gmail.eml")  # in the inbox before OpenBerry was connected
    assert services.check_inbox(now=NOW) == {"status": "ok", "checked": 0, "recorded": 0}
    assert repo.get_lead(lina).status == "contacted" and "UID FETCH" not in box.imap_commands()


def test_a_reply_is_logged_once_and_stops_both_queues(company, box):
    turn_on(company)
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    lina, _ = lina_sent(company, box, linkedin_url="https://www.linkedin.com/in/lina-noor")
    follow_up = approved(lina, step=2)
    dm = approved(lina, channel="linkedin_dm", step=2, body="Hi again")
    deliver(box, "reply_gmail.eml")
    assert services.check_inbox(now=NOW + timedelta(minutes=5)) == {"status": "ok", "checked": 1, "recorded": 1}
    replies = [m for m in repo.list_messages(company.id, lead_id=lina) if m.direction == "inbound"]
    assert [(m.channel, m.generated_by) for m in replies] == [("email", "inbox")]
    assert "corporate rates" in replies[0].body and "Saw you are hiring" not in replies[0].body
    assert repo.get_lead(lina).status == "replied"
    assert repo.get_message(follow_up.id).status == "skipped" and repo.get_message(dm.id).status == "skipped"
    assert [e["kind"] for e in repo.list_email_events(lead_id=lina)] == ["reply"]
    # Seen again (the same message in the junk folder, or after the server renumbered the inbox): logged once.
    deliver(box, "reply_gmail.eml", folder="Junk")
    box.folders["INBOX"].uidvalidity = 99
    services.check_inbox(now=NOW + timedelta(minutes=10))
    assert len([m for m in repo.list_messages(company.id, lead_id=lina) if m.direction == "inbound"]) == 1
    assert not set(box.imap_commands()) - {"connect", "LOGIN", "LIST", "STATUS", "EXAMINE", "UID SEARCH",
                                           "UID FETCH", "LOGOUT", "APPEND"}


@pytest.mark.parametrize("name", ["stop.eml", "optout_arabic.eml", "unsubscribe_list.eml"])
def test_an_opt_out_suppresses_disqualifies_and_tags(company, box, name):
    turn_on(company)
    lina, _ = lina_sent(company, box)
    deliver(box, name)
    services.check_inbox(now=NOW + timedelta(minutes=5))
    found = repo.get_lead(lina)
    assert found.status == "disqualified" and "unsubscribed" in found.tags
    assert ("Asked not to be contacted (the Unsubscribe button)" if name == "unsubscribe_list.eml"
            else "Asked not to be contacted (email reply)") in found.notes
    assert repo.suppressed_addresses(["lina@gulfbank.example"])["lina@gulfbank.example"]["reason"] == "opt_out"
    assert [e["kind"] for e in repo.list_email_events(lead_id=lina)] == ["opt_out"]
    # For every company, even if the lead is deleted and found again.
    repo.delete_lead(lina)
    other = turn_on(repo.create_company(CompanyIn(name="Second Co")))
    again = approved(lead(other, "Lina@GulfBank.example"))
    assert "asked not to be emailed" in skip_reason(other, again.id)


def test_a_company_lead_that_opts_out_goes_on_the_never_contact_list_when_you_say_so(company, box):
    turn_on(company)
    hotel = repo.upsert_lead(company.id, LeadIn(lead_company="Palm Hotel", email="lina@gulfbank.example"))[0]
    sent_from_mailbox(hotel.id)
    services.check_inbox(now=NOW)
    deliver(box, "stop.eml")
    services.check_inbox(now=NOW + timedelta(minutes=5))
    # Read in the inbox: the address is never emailed again and the lead is tagged, but the never-contact list
    # (which also stops LinkedIn and future leads) is left to the user: words in an email can be misread.
    found = repo.get_lead(hotel.id)
    assert found.status == "disqualified" and "unsubscribed" in found.tags
    assert "Palm Hotel" not in repo.get_company(company.id).icp.exclude_companies
    repo.record_opt_out(hotel.id, "marked in the dashboard", "You marked it")  # the lead page's Unsubscribed
    assert "Palm Hotel" in repo.get_company(company.id).icp.exclude_companies


def test_an_opt_out_read_in_the_inbox_never_changes_a_won_customer(company, box):
    turn_on(company)
    hotel = repo.upsert_lead(company.id, LeadIn(lead_company="Palm Hotel", email="lina@gulfbank.example"))[0]
    sent_from_mailbox(hotel.id)
    repo.update_lead(hotel.id, {"status": "won"})
    services.check_inbox(now=NOW)
    box.deliver(f"From: lina@gulfbank.example\r\nTo: {FROM}\r\nSubject: Re: Airport transfers\r\n"
                f"Message-ID: <won-1@gulfbank.example>\r\nIn-Reply-To: {OUR_ID}\r\n\r\n"
                "Please remove me from your mailing list.\r\n".encode())
    services.check_inbox(now=NOW + timedelta(minutes=5))
    found = repo.get_lead(hotel.id)
    assert found.status == "won" and "unsubscribed" in found.tags
    assert repo.suppressed_addresses(["lina@gulfbank.example"])["lina@gulfbank.example"]["reason"] == "opt_out"
    assert repo.get_company(company.id).icp.exclude_companies == []


def test_a_hard_bounce_marks_the_email_and_suppresses_the_address(company, box):
    turn_on(company)
    for name in ("dsn_postfix.eml", "dsn_exchange.eml", "dsn_gmail.eml", "bounce_nonstandard.eml"):
        lina, message = lina_sent(company, box)
        deliver(box, name)
        assert services.check_inbox(now=NOW + timedelta(minutes=5))["recorded"] == 1, name
        stored = repo.get_message(message.id)
        assert stored.email_state == "bounced" and stored.status == "sent" and "5.1." in stored.send_error, name
        assert "email-bounced" in repo.get_lead(lina).tags
        assert repo.suppressed_addresses(["lina@gulfbank.example"])["lina@gulfbank.example"]["reason"] == "bounce"
        with db.connect() as c:  # start the next fixture afresh
            c.execute("DELETE FROM email_suppressions")
            c.execute("UPDATE messages SET email_message_id = NULL")
        repo.delete_lead(lina)
        box.folders["INBOX"].messages.clear()
        with db.connect() as c:
            c.execute("UPDATE email_mailbox SET imap_state = '{}'")
    # Three hard bounces in 24 hours paused email sending for the company.
    assert repo.get_company(company.id).outreach.email_paused_until is not None


def test_soft_bounces_policy_rejections_and_auto_replies_dont_stop_the_sequence(company, box):
    turn_on(company)
    lina, message = lina_sent(company, box)
    follow_up = approved(lina, step=2)
    for name in ("dsn_delayed.eml", "ooo_autosubmitted.eml", "ooo_subject.eml", "dsn_policy.eml"):
        deliver(box, name)
    services.check_inbox(now=NOW + timedelta(minutes=5))
    assert repo.get_lead(lina).status == "contacted" and repo.get_message(follow_up.id).status == "approved"
    assert sorted(e["kind"] for e in repo.list_email_events(lead_id=lina)) == ["auto_reply", "auto_reply",
                                                                              "policy_reject", "soft_bounce"]
    assert repo.suppressed_addresses(["lina@gulfbank.example"]) == {}  # a policy rejection is about the sender
    assert repo.get_message(message.id).email_state == "sent"


def test_mail_about_no_lead_is_never_stored_and_a_bad_message_never_stops_the_check(company, box):
    turn_on(company)
    lina, _ = lina_sent(company, box)
    deliver(box, "unrelated.eml")
    box.deliver(eml("malformed.eml"))
    deliver(box, "reply_outlook.eml")
    result = services.check_inbox(now=NOW + timedelta(minutes=5))
    assert result["status"] == "ok" and result["recorded"] == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM messages WHERE direction = 'inbound'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 1
    assert "promo" not in json.dumps([dict(r) for r in repo.list_email_events()], default=str)


def test_a_reply_to_an_email_that_may_have_been_sent_resolves_it(company, box):
    turn_on(company)
    lina, message = lina_sent(company, box)
    set_row(message.id, status="approved", sent_at=None, sent_via="", email_state="unknown")
    deliver(box, "reply_apple.eml")
    services.check_inbox(now=NOW + timedelta(minutes=5))
    stored = repo.get_message(message.id)
    assert (stored.status, stored.email_state, stored.sent_via) == ("sent", "sent", "smtp") and stored.sent_at == NOW
    assert {e["kind"] for e in repo.list_email_events(lead_id=lina)} == {"resolved", "reply"}


def test_a_reply_to_an_email_sent_by_hand_is_found_by_its_sender(company, box):
    lina = lead(company, "lina@gulfbank.example")
    by_hand = approved(lina)
    repo.update_message(by_hand.id, status="sent")  # the user's own Mark sent: no Message-ID known
    services.check_inbox(now=NOW)
    box.deliver(eml("reply_gmail.eml", lead="lina@gulfbank.example", ours="<from-their-own-app@x.example>"))
    services.check_inbox(now=NOW + timedelta(minutes=5))
    assert repo.get_lead(lina).status == "replied"


def test_a_failing_inbox_check_blocks_sending(company, box):
    import imaplib

    turn_on(company)
    approved(lead(company))
    box.imap_fail["connect"] = ConnectionRefusedError(111, "Connection refused")
    result = services.check_inbox(now=NOW)
    assert result["status"] == "failed" and result["kind"] == mailer.RETRY
    assert repo.email_sending_status(company.id, now=NOW)["blocked_reason"] == "inbox_unchecked"
    assert services.send_next_email(now=NOW) == {"status": "idle"} and box.sent == []
    assert services.inbox_due(now=NOW + timedelta(minutes=4)) is False
    assert services.inbox_due(now=NOW + timedelta(minutes=5)) is True  # a network problem: tried again


def test_a_refused_inbox_login_is_not_tried_again_until_the_user_acts(company, box):
    import imaplib

    turn_on(company)
    approved(lead(company))
    box.imap_fail["login"] = imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
    result = services.check_inbox(now=NOW)
    assert result["status"] == "failed" and result["kind"] == mailer.AUTH
    state = repo.mailbox_state(FROM, now=NOW)
    assert state["auth_failed"] and state["paused"] and "for reading mail" in state["pause_reason"]
    # Providers lock an account that keeps trying a wrong password: no automatic check, no email, for hours.
    for minutes in (5, 60, 600):
        assert services.inbox_due(now=NOW + timedelta(minutes=minutes)) is False
    assert services.send_next_email(now=NOW + timedelta(hours=1)) == {"status": "idle"} and box.sent == []
    assert box.imap_commands().count("LOGIN") == 1
    box.imap_fail.clear()
    assert repo.retry_mailbox(FROM, now=NOW + timedelta(hours=1), auth_only=True) is True  # saved, or Check inbox
    assert services.inbox_due(now=NOW + timedelta(hours=1)) is True
    # A refused SMTP login stops the automatic inbox checks too (usually the same password).
    box.fail["login"] = smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")
    assert send(NOW + timedelta(hours=1))["status"] == mailer.AUTH
    assert services.inbox_due(now=NOW + timedelta(hours=2)) is False


def test_gmail_keeps_its_own_sent_copy(company, settings, offline_mail):
    connect_mailbox(settings, provider="gmail", save_sent=False)
    turn_on(company)
    approved(lead(company))
    assert send()["status"] == "sent" and offline_mail.appended == []


def test_a_test_email_goes_to_the_mailbox_itself_only(company, box):
    outcome = services.send_test_email()
    assert outcome.ok and [s["to"] for s in box.sent] == [FROM]
    assert repo.email_sending_status(company.id, now=NOW)["sent_last_24h"] == 0  # not counted


# --------------------------------------------------------------------------------------
# The email loop
# --------------------------------------------------------------------------------------


async def test_the_email_loop_checks_the_inbox_sends_one_and_wakes_on_send_now(company, box, monkeypatch):
    import asyncio

    from openberry import scheduler

    calls: list[str] = []
    monkeypatch.setattr(services, "inbox_due", lambda now=None: calls.append("due") or True)
    monkeypatch.setattr(services, "check_inbox", lambda now=None: calls.append("check") or {"status": "ok"})
    monkeypatch.setattr(services, "send_next_email", lambda now=None: calls.append("send") or {"status": "idle"})
    stop, wake = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(scheduler.email_loop(stop, wake, tick=60))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls.count("send") == 1:
            break
    wake.set()  # Send now
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls.count("send") == 2:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert calls[:3] == ["due", "check", "send"] and calls.count("send") == 2


# --------------------------------------------------------------------------------------
# Schema version 5
# --------------------------------------------------------------------------------------


def test_a_version_4_database_gets_the_email_columns_and_tables(settings):
    import re

    lines = [line for line in db.SCHEMA.split("-- Email sending (schema version 5)")[0].split("\n")
             if not line.strip().startswith(tuple(db.EMAIL_MESSAGE_COLUMNS)) and "version 5" not in line]
    v4 = re.sub(r",([ \t]*(?:--[^\n]*)?)\n\);", r"\1\n);", "\n".join(lines))
    assert "email_state" not in v4 and "auto_blocked" in v4 and "email_suppressions" not in v4
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(v4)
    now = repo.iso()
    conn.execute("INSERT INTO companies (name, outreach, created_at, updated_at) VALUES ('Old Co', ?, ?, ?)",
                 (json.dumps({"sender_name": "Sam", "auto_approve": True}), now, now))
    conn.execute("INSERT INTO leads (company_id, full_name, email, created_at, updated_at) "
                 "VALUES (1, 'Old Lead', 'old@lead.example', ?, ?)", (now, now))
    conn.execute("INSERT INTO messages (company_id, lead_id, channel, subject, body, status, created_at, updated_at, "
                 "approved_via) VALUES (1, 1, 'email', 'Hi', 'Old email', 'approved', ?, ?, 'auto')", (now, now))
    conn.execute("PRAGMA user_version=4")
    conn.commit()
    conn.close()
    db.reset_init_cache()
    with db.connect() as c:
        columns = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        assert set(db.EMAIL_MESSAGE_COLUMNS) <= columns and "approved_via" in columns
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"email_suppressions", "email_events", "email_mailbox"} <= tables
        indexes = {r[1] for r in c.execute("PRAGMA index_list(messages)")}
        assert {"ix_messages_email_mid", "ix_messages_email_state"} <= indexes
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 6
        assert "email_sent_log" in tables and {"email_from", "opt_out_hint"} <= columns
    old = repo.list_messages(1)[0]
    assert (old.email_state, old.email_message_id, old.approved_via, old.status) == ("", None, "auto", "approved")
    assert repo.get_company(1).outreach.email_sending is False  # an old profile: off


# --------------------------------------------------------------------------------------
# One address is one recipient; earlier steps first; changes made while an email is being sent
# --------------------------------------------------------------------------------------


ROVE = "reservations@rovehotels.example"


def test_an_address_on_several_leads_gets_one_cold_email_and_a_reply_stops_them_all(company, box):
    turn_on(company)
    # Two hotel branches whose Google Maps listings point to one chain's site: two account leads, one mailbox.
    downtown = repo.upsert_lead(company.id, LeadIn(lead_company="Rove Downtown", email=ROVE))[0]
    city_walk = repo.upsert_lead(company.id, LeadIn(lead_company="Rove City Walk",
                                                    email="Reservations@RoveHotels.example"))[0]
    first, second = approved(downtown.id), approved(city_walk.id)
    assert repo.EMAIL_ONE_AT_A_TIME in skip_reason(company, second.id)
    assert send()["message_id"] == first.id
    assert send(NOW + timedelta(minutes=6)) == {"status": "idle"} and len(box.sent) == 1
    assert f"step 1 was already emailed to {ROVE} through another lead" in skip_reason(
        company, second.id, NOW + timedelta(minutes=6))
    # The same address as a person lead in another company: the same mailbox, so it waits too.
    other = turn_on(repo.create_company(CompanyIn(name="Second Co")))
    person = approved(lead(other, ROVE, full_name="Omar Front Desk"))
    assert "through another lead" in skip_reason(other, person.id, NOW + timedelta(minutes=6))
    with db.connect() as c:  # and the claim's own check refuses it, whatever the queue said
        assert repo._claim_email(c, repo.get_company(other.id), person.id, "x", FROM, "",
                                 NOW + timedelta(minutes=6)) is None
    # The hotel replies through the first lead: the other leads' follow-ups never go to the address that replied.
    repo.update_message(second.id, status="skipped")
    follow_up = approved(city_walk.id, step=2)
    repo.log_reply(downtown.id, "Thanks, we have a provider.", channel="email")
    assert repo.get_lead(city_walk.id).status == "new"
    assert repo.ADDRESS_REPLIED in skip_reason(company, follow_up.id, NOW + timedelta(days=10))
    assert send(NOW + timedelta(days=10)) == {"status": "idle"} and len(box.sent) == 1
    with db.connect() as c:
        assert repo._claim_email(c, repo.get_company(company.id), follow_up.id, "y", FROM, "",
                                 NOW + timedelta(days=10)) is None


def test_a_follow_up_never_goes_out_as_the_first_contact(company, box):
    turn_on(company)
    one = lead(company)
    step_one = approved(one, status="draft")
    follow_up = approved(one, step=2, body="Hi, just following up on my email from last week.")
    assert "step 1 to this lead isn't sent yet: send or skip it first" in skip_reason(company, follow_up.id)
    two = lead(company, linkedin_url="https://www.linkedin.com/in/two-first")
    approved(two, channel="linkedin_connect", body="Hi, glad to connect.")  # approved, not sent yet
    after_linkedin = approved(two, step=2)
    assert "step 1 to this lead isn't sent yet" in skip_reason(company, after_linkedin.id)
    assert send() == {"status": "idle"} and box.sent == []
    repo.update_message(step_one.id, status="skipped")  # a skipped step doesn't hold it back
    assert send()["message_id"] == follow_up.id


@pytest.mark.parametrize("change", ["lost", "never_contact", "auto_switch_off", "unsubscribed"])
def test_a_change_made_while_the_email_is_being_sent_stops_it_before_data(company, box, monkeypatch, change):
    from email_fakes import _FakeSMTPBase

    turn_on(company, email_send_auto_approved=True)
    target = lead(company, lead_company="Gulf Bank")
    message = approved(target)
    set_row(message.id, approved_via="auto")
    real_rcpt = _FakeSMTPBase.rcpt

    def user_acts_meanwhile(self: Any, recip: str, options: Any = ()) -> Any:  # while OpenBerry is signed in
        if change == "lost":
            repo.update_lead(target, {"status": "lost"})
        elif change == "never_contact":
            repo.update_company(company.id, {"icp": {"exclude_companies": ["Gulf Bank"]}})
        elif change == "auto_switch_off":
            repo.update_company(company.id, {"outreach": {"email_send_auto_approved": False}})
        else:
            repo.update_lead(target, {"tags": ["unsubscribed"]})
        return real_rcpt(self, recip, options)

    monkeypatch.setattr(_FakeSMTPBase, "rcpt", user_acts_meanwhile)
    assert send()["status"] == mailer.ABORTED
    assert box.sent == [] and "rset" in box.smtp_commands() and "data" not in box.smtp_commands()
    assert row(message.id)["email_state"] == ""


def test_a_new_address_during_a_send_never_gets_the_email(company, box, monkeypatch):
    from email_fakes import _FakeSMTPBase

    turn_on(company)
    target = lead(company, "old@client.example")
    message = approved(target)
    real_rcpt = _FakeSMTPBase.rcpt

    def address_changed_then_451(self: Any, recip: str, options: Any = ()) -> Any:
        repo.update_lead(target, {"email": "someone.else@other.example"})
        return 451, b"4.7.1 Try again later"

    monkeypatch.setattr(_FakeSMTPBase, "rcpt", address_changed_then_451)
    assert send()["status"] == mailer.RETRY
    stored = repo.get_message(message.id)
    assert (stored.status, stored.auto_hold, stored.email_state) == ("draft", True, "")  # nobody approved the new one
    monkeypatch.setattr(_FakeSMTPBase, "rcpt", real_rcpt)
    repo.retry_mailbox(FROM)
    assert send(NOW + timedelta(minutes=10)) == {"status": "idle"} and box.sent == []
    # The same after a crash: a stale claim goes back as a draft, not as an approval for the old address.
    other = lead(company, "a@client.example")
    second = approved(other)
    assert repo.claim_next_email("crashed", FROM, now=NOW + timedelta(minutes=20)).message_id == second.id
    repo.update_lead(other, {"email": "b@client.example"})
    repo.expire_email_claims(now=NOW + timedelta(minutes=31))
    assert repo.get_message(second.id).status == "draft"


def test_skipping_an_email_that_may_have_been_sent_keeps_the_follow_up_wait(company, box):
    turn_on(company)
    target = lead(company)
    first = approved(target)
    box.fail["data"] = TimeoutError("timed out")
    assert send()["status"] == mailer.UNKNOWN
    box.fail.clear()
    repo.retry_mailbox(FROM)
    repo.update_message(first.id, status="skipped")  # Skip, next to Mark sent and Send again
    again = approved(target, subject="Another try")
    follow_up = approved(target, step=2)
    later = NOW + timedelta(minutes=10)
    assert send(later) == {"status": "idle"} and box.sent == []
    assert "step 1 was already emailed to this lead" in skip_reason(company, again.id, later)
    assert "not due before" in skip_reason(company, follow_up.id, later)
    assert [m.id for m in repo.email_attention(company.id)["messages"]] == [first.id]  # it still needs you


def test_an_opt_out_sticks_to_the_lead_whatever_address_it_gets(company, box):
    turn_on(company)
    lina = lead(company, "lina@gulfbank.example")
    repo.record_opt_out(lina, "marked in the dashboard", "You marked it")  # the lead page's Unsubscribed
    repo.update_lead(lina, {"email": "lina.noor@gulfbank.example", "status": "qualified"})  # Claude or the API
    message = repo.update_message_as(approved(lina, status="draft").id, "claude", status="approved")
    assert repo.UNSUBSCRIBED in skip_reason(company, message.id)
    assert send() == {"status": "idle"} and box.sent == []


def test_claude_marking_a_maybe_sent_email_as_sent_keeps_it_counted(company, box):
    turn_on(company, email_daily_limit=1)
    first, second = approved(lead(company)), approved(lead(company))
    box.fail["data"] = TimeoutError("timed out")
    assert send()["status"] == mailer.UNKNOWN
    box.fail.clear()
    repo.retry_mailbox(FROM)
    stored = repo.update_message_as(first.id, "claude", status="sent", now=NOW + timedelta(minutes=8))
    assert (stored.sent_via, stored.email_state, stored.sent_at) == ("smtp", "sent", NOW)
    later = NOW + timedelta(minutes=10)
    assert repo.email_sending_status(company.id, now=later)["blocked_reason"] == "daily_limit"
    assert send(later) == {"status": "idle"} and box.sent == [] and repo.get_message(second.id).sent_at is None


def test_the_mailbox_itself_is_never_a_recipient_and_its_own_email_is_never_read_as_a_reply(company, box):
    turn_on(company)
    me = lead(company, FROM.upper(), full_name="Sam Test")
    message = approved(me)
    assert repo.OWN_MAILBOX in skip_reason(company, message.id)
    assert send() == {"status": "idle"} and box.sent == []
    # Sent anyway (by an older version): it comes back to the inbox, with the footer's "unsubscribe" in it.
    sent_from_mailbox(me)
    services.check_inbox(now=NOW)
    box.deliver(f"From: Sam Rahman <{FROM}>\r\nTo: {FROM}\r\nSubject: Airport transfers\r\nMessage-ID: {OUR_ID}\r\n"
                f"\r\n{BODY}\r\n\r\n{FOOTER}\r\n".replace("\n", "\r\n").replace("\r\r\n", "\r\n").encode())
    assert services.send_test_email().ok  # the test email arrives too
    box.deliver(box.sent[-1]["data"])
    assert services.check_inbox(now=NOW + timedelta(minutes=5)) == {"status": "ok", "checked": 0, "recorded": 0}
    found = repo.get_lead(me)
    assert found.status == "contacted" and "unsubscribed" not in found.tags
    assert repo.suppressed_addresses([FROM]) == {}


def test_a_reply_that_cant_be_recorded_is_read_again_and_sending_waits(company, box, monkeypatch):
    turn_on(company)
    lina, _ = lina_sent(company, box)
    follow_up = approved(lina, step=2)
    deliver(box, "stop.eml")
    real = repo.record_inbound

    def busy(*args: Any, **kwargs: Any) -> Any:  # another writer held the lock past the busy timeout
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "record_inbound", busy)
    result = services.check_inbox(now=NOW + timedelta(minutes=5))
    assert result["status"] == "failed" and "couldn't save a message from your inbox" in result["text"]
    assert repo.email_sending_status(company.id, now=NOW + timedelta(minutes=5))["blocked_reason"] == \
        "inbox_unchecked"
    monkeypatch.setattr(repo, "record_inbound", real)
    assert services.check_inbox(now=NOW + timedelta(minutes=10)) == {"status": "ok", "checked": 1, "recorded": 1}
    assert repo.get_lead(lina).status == "disqualified" and repo.get_message(follow_up.id).status == "skipped"
    assert repo.suppressed_addresses(["lina@gulfbank.example"])["lina@gulfbank.example"]["reason"] == "opt_out"


def test_a_reply_without_a_message_id_is_logged_once_after_the_folder_is_renumbered(company, box):
    import re

    turn_on(company)
    lina, _ = lina_sent(company, box)
    raw = re.sub(rb"Message-ID: [^\r\n]*\r\n", b"", eml("reply_gmail.eml", lead="lina@gulfbank.example", ours=OUR_ID),
                 flags=re.I)
    box.deliver(raw)
    assert services.check_inbox(now=NOW + timedelta(minutes=5))["recorded"] == 1
    box.folders["INBOX"].uidvalidity = 77
    box.deliver(raw, "Junk")
    services.check_inbox(now=NOW + timedelta(minutes=10))
    assert len([m for m in repo.list_messages(company.id, lead_id=lina) if m.direction == "inbound"]) == 1


def test_a_newsletter_from_a_hotels_address_changes_nothing(company, box):
    turn_on(company)
    hotel = repo.upsert_lead(company.id, LeadIn(lead_company="Palm Hotel", email="lina@gulfbank.example"))[0]
    sent_from_mailbox(hotel.id)
    services.check_inbox(now=NOW)
    box.deliver(f"From: Palm Hotel <lina@gulfbank.example>\r\nTo: {FROM}\r\nSubject: Autumn offers\r\n"
                "Message-ID: <campaign-9@gulfbank.example>\r\nList-Unsubscribe: <https://example.com/u>\r\n\r\n"
                "New suites. Unsubscribe here.\r\n".encode())
    services.check_inbox(now=NOW + timedelta(minutes=5))
    assert repo.get_lead(hotel.id).status == "contacted" and repo.suppressed_addresses(["lina@gulfbank.example"]) == {}
    assert [e["kind"] for e in repo.list_email_events(lead_id=hotel.id)] == ["auto_reply"]


@pytest.mark.parametrize("sep", [" ", "\x85", "\x0b"])
def test_a_subject_with_an_unusual_line_break_is_held_back_before_any_claim(company, box, sep):
    turn_on(company)
    message = approved(lead(company), subject=f"Airport transfers{sep}for Gulf Bank")
    assert skip_reason(company, message.id) == "the subject has a line break"
    assert send() == {"status": "idle"} and box.smtp_commands() == []
