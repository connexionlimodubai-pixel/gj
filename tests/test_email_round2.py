"""Email sending, round 2 of the review: one test per finding (ids in the docstrings), each failing without its fix,
and the email parts of WP5 (the sent-email log that outlives deletions, a clock that went back).

Mail servers are fakes (email_fakes.py): no network. tests/fixtures/email/corpus holds the reviewers' real-world
replies (Gmail, Outlook in four languages, Zoho, Apple Mail, Arabic, bounces from Exim, Postfix, Microsoft 365...),
with example names: manifest.json says what each one is.
"""

from __future__ import annotations

import imaplib
import json
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from email_fakes import FROM, OUR_ID, connect_mailbox, eml
from openberry import config, db, mailer, mcp_server, repo, services
from openberry.models import Company, CompanyIn, LeadIn, Message

DUBAI = timezone(timedelta(hours=4))
NOW = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)  # Monday 10:00 in Dubai: inside the default sending hours
FOOTER = ('Acme Chauffeurs · Office 1204, Bay Square, Business Bay, Dubai\n'
          'Not interested? Reply "unsubscribe" and we won\'t email you again.')
BODY = "Hi Lina,\n\nWe drive executives to and from DXB.\n\nSam"
LINA = "lina@gulfbank.example"
ALIAS = "bookings@acme-mail.example"
CORPUS = Path(__file__).parent / "fixtures" / "email" / "corpus"
MANIFEST = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
CORPUS_ITEMS = {item["file"]: item for item in MANIFEST["messages"]}


@pytest.fixture(autouse=True)
def dubai_clock(monkeypatch: pytest.MonkeyPatch) -> None:
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
    return repo.update_company(company.id, {"outreach": {"email_sending": True, "email_footer": FOOTER, **outreach}})


def lead(company: Company, email_address: str | None = None, **fields: Any) -> int:
    n = next(_people)
    data = {"full_name": f"Person {n}", "title": "Travel Manager", "lead_company": f"Client {n}",
            "email": f"person{n}@client{n}.example" if email_address is None else email_address, **fields}
    return repo.upsert_lead(company.id, LeadIn(**data))[0].id


def approved(lead_id: int, step: int = 1, status: str = "approved", subject: str = "Airport transfers for your guests",
             body: str = BODY) -> Message:
    return repo.create_message(lead_id, body, channel="email", subject=subject, step=step, status=status,
                               generated_by="claude")


def send(now: datetime = NOW, fresh: bool = True) -> dict[str, Any]:
    if fresh:
        repo.save_inbox_check(FROM, True, now=now)
    return services.send_next_email(now=now)


def set_row(message_id: int, **fields: Any) -> None:
    with db.connect() as c:
        c.execute(f"UPDATE messages SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                  [*fields.values(), message_id])


def sent_from_mailbox(lead_id: int, header_id: str = OUR_ID, when: datetime = NOW, step: int = 1) -> Message:
    msg = approved(lead_id, step=step)
    set_row(msg.id, status="sent", sent_at=repo.iso(when), sent_via="smtp", email_state="sent",
            email_message_id=header_id, email_to=repo.get_lead(lead_id).email, email_claimed_at=repo.iso(when))
    repo.update_lead(lead_id, {"status": "contacted"})
    return repo.get_message(msg.id)


def lina_sent(company: Company, **fields: Any) -> tuple[int, Message]:
    """Lina got an email from OpenBerry (Message-ID OUR_ID) two hours ago, and the inbox was checked once since."""
    lina = lead(company, LINA, full_name="Lina Noor", **fields)
    message = sent_from_mailbox(lina, when=NOW - timedelta(hours=2))
    assert services.check_inbox(now=NOW)["status"] == "ok"
    return lina, message


def maybe_sent(company: Company, box, **outreach: Any) -> tuple[int, Message]:
    """Lina's step 1 may have been sent: the connection broke while the server was receiving it, at NOW."""
    turn_on(company, **outreach)
    lina = lead(company, LINA, full_name="Lina Noor")
    msg = approved(lina)
    box.fail["data"] = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    assert send(NOW)["status"] == mailer.UNKNOWN
    box.fail.clear()
    return lina, repo.get_message(msg.id)


def re_send_blocked(box, when: datetime) -> None:
    """The re-send after Send again fails for good: the provider's daily quota."""
    box.fail["rcpt"] = (550, b"5.4.5 Daily user sending quota exceeded")
    assert send(when)["status"] == mailer.PROVIDER_BLOCK
    box.fail.clear()


def deliver(world, name: str, folder: str = "INBOX", **extra: str) -> None:
    world.deliver(eml(name, lead=LINA, ours=OUR_ID, **extra), folder)


def reply(text: str, sender: str = LINA, subject: str = "Re: Airport transfers for your guests",
          refers: str = OUR_ID, name: str = "Lina Noor", msg_id: str = "") -> bytes:
    head = (f"From: {name} <{sender}>\r\nTo: {FROM}\r\nSubject: {subject}\r\n"
            f"Message-ID: <{msg_id or abs(hash(text + sender))}@mail.example>\r\n")
    if refers:
        head += f"In-Reply-To: {refers}\r\nReferences: {refers}\r\n"
    return (head + "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + text + "\r\n").encode()


def suppressed(*addresses: str) -> dict[str, str]:
    return {k: v["reason"] for k, v in repo.suppressed_addresses(list(addresses)).items()}


def skip_reason(company: Company, message_id: int, now: datetime = NOW) -> str:
    return {s["message_id"]: s["reason"] for s in repo.email_queue(company.id, now=now)["skipped"]}[message_id]


# --------------------------------------------------------------------------------------
# The mailbox: its state is kept per account, refusals are told apart (R2-1, R2-2, R2-5, R2-6, R2-11, R2UI-1)
# --------------------------------------------------------------------------------------


def test_a_settings_problem_with_sending_never_stops_the_inbox_checks(company, box):
    """R2-1: the From address refused at MAIL FROM pauses sending, but replies and opt-outs are still read, and the
    opt-out that arrives meanwhile stops the follow-up. A refused login still stops the checks."""
    turn_on(company)
    lina, _ = lina_sent(company)
    follow_up = approved(lina, step=2)
    set_row(follow_up.id, created_at=repo.iso(NOW - timedelta(days=1)))
    other = approved(lead(company))
    box.fail["mail"] = (553, b"5.7.1 Sender address rejected: not owned by user")
    assert send(NOW)["status"] == mailer.CONFIG
    state = repo.mailbox_state(FROM, now=NOW)
    assert state["paused"] and state["auth_failed"] and not state["login_refused"]
    assert "won't send from" in state["pause_reason"]
    assert services.inbox_due(now=NOW + timedelta(minutes=6)) is True
    deliver(box, "stop.eml")
    services.check_inbox(now=NOW + timedelta(minutes=6))
    assert repo.get_lead(lina).status == "disqualified" and suppressed(LINA) == {LINA: "opt_out"}
    assert repo.get_message(follow_up.id).status != "approved" and repo.get_message(other.id).status == "approved"
    repo.record_email_failure(0, "", mailer.AUTH, "535 refused", FROM)
    assert services.inbox_due(now=NOW + timedelta(minutes=20)) is False


@pytest.mark.parametrize("refused", [
    (550, b"5.1.0 <sam@acme-mail.example>: Sender address rejected: User unknown in virtual mailbox table"),
    (553, b"5.7.1 <sam@acme-mail.example>: Sender address rejected: not owned by user sam@acme-mail.example"),
    (553, b"5.1.8 <sam@acme-mail.example>: Sender address rejected: Domain not found"),
])
def test_the_from_address_refused_at_rcpt_to_is_a_setting_not_a_bounce(company, box, refused):
    """EMAIL-5: Postfix refuses a From address it doesn't allow in reply to RCPT TO (smtpd_delay_reject). Sending
    waits for the settings, as at MAIL FROM: Lina's address isn't put on the never-email list, her lead isn't tagged,
    and the mailbox isn't paused for 24 hours. The test email says the same."""
    turn_on(company)
    lina = lead(company, LINA, full_name="Lina Noor")
    msg = approved(lina)
    box.fail["rcpt"] = refused
    assert send(NOW)["status"] == mailer.CONFIG
    assert suppressed(LINA) == {} and repo.get_lead(lina).tags == []
    assert repo.get_message(msg.id).status == "approved" and repo.get_message(msg.id).email_state == ""
    state = repo.mailbox_state(FROM, now=NOW)
    assert state["auth_failed"] and "won't send from" in state["pause_reason"] and state["paused_until"] is None
    assert [e["kind"] for e in repo.list_email_events(lead_id=lina)] == []
    outcome = services.send_test_email()
    assert outcome.kind == mailer.CONFIG and "won't send from" in outcome.text


def test_a_dns_failure_at_send_time_is_tried_again_by_itself(company, box):
    """R2-1 (optional part): a Wi-Fi drop makes the server's name unknown for a moment: tried again in 5, 15, 60
    minutes, never a pause that waits for the user."""
    import socket

    turn_on(company)
    approved(lead(company))
    box.fail["connect"] = socket.gaierror(-3, "Temporary failure in name resolution")
    assert send(NOW)["status"] == mailer.RETRY
    state = repo.mailbox_state(FROM, now=NOW)
    assert not state["auth_failed"] and state["paused_until"] == repo.iso(NOW + timedelta(minutes=5))
    box.fail.clear()
    assert send(NOW + timedelta(minutes=6))["status"] == "sent"


def test_changing_the_from_address_keeps_the_inbox_position_and_the_pauses(company, settings, box):
    """R2-2, R2-5: the state (where the inbox check stopped, a provider's 24-hour block) is kept per account, so saving
    an alias as the From address neither skips the opt-out that arrived meanwhile nor lifts the block."""
    turn_on(company)
    lina, _ = lina_sent(company)
    follow_up = approved(lina, step=2)
    deliver(box, "stop.eml")  # arrives while OpenBerry is closed
    repo.record_email_failure(0, "", mailer.PROVIDER_BLOCK, "554 5.7.1 your account is suspended for sending spam",
                              FROM, now=NOW)
    settings.smtp_from_email = ALIAS  # the user saves an alias (same account, same username)
    repo.retry_mailbox(auth_only=True, now=NOW + timedelta(minutes=1))
    state = repo.mailbox_state(now=NOW + timedelta(minutes=10))
    assert state["address"] == ALIAS and state["paused"] and "suspended" in state["pause_reason"]
    result = services.check_inbox(now=NOW + timedelta(minutes=10))
    assert result == {"status": "ok", "checked": 1, "recorded": 1}
    assert repo.get_lead(lina).status == "disqualified" and repo.get_message(follow_up.id).status != "approved"
    approved(lead(company))
    assert services.send_next_email(now=NOW + timedelta(minutes=10)) == {"status": "idle"} and box.sent == []


def test_a_new_reading_server_reads_the_mail_since_the_last_check(company, settings, box):
    """R2-2: positions are kept per IMAP server and username. With new details, the first check reads the mail since
    the last check instead of nothing, and a message read before is never recorded twice."""
    turn_on(company)
    lina, _ = lina_sent(company)
    services.check_inbox(now=NOW + timedelta(minutes=5))
    deliver(box, "stop.eml")
    settings.imap_host = "imappro.acme-mail.example"  # the same mailbox through another server name
    result = services.check_inbox(now=NOW + timedelta(minutes=10))
    assert result["status"] == "ok" and result["recorded"] == 1
    assert repo.get_lead(lina).status == "disqualified"
    assert services.check_inbox(now=NOW + timedelta(minutes=15))["recorded"] == 0


def test_state_saved_under_the_from_address_before_is_kept(company, settings, box):
    """R2-2: a mailbox row written before the state was kept per account (under the From address) is carried over."""
    settings.smtp_username = "sam.login@acme-mail.example"  # signs in with another name than the From address
    with db.connect() as c:
        c.execute("DELETE FROM email_mailbox")
        c.execute("INSERT INTO email_mailbox (address, paused_until, until_reason, updated_at) VALUES (?, ?, ?, ?)",
                  (FROM, repo.iso(NOW + timedelta(hours=20)), "554 suspended", repo.iso(NOW)))
    state = repo.mailbox_state(now=NOW)
    assert state["paused"] and state["pause_reason"] == "554 suspended"
    repo.save_inbox_check(FROM, True, now=NOW)  # the first write keeps it under the account
    with db.connect() as c:
        row = c.execute("SELECT paused_until FROM email_mailbox WHERE address = ?",
                        ("sam.login@acme-mail.example",)).fetchone()
    assert row["paused_until"] == repo.iso(NOW + timedelta(hours=20))


def test_a_temporary_imap_authentication_failure_is_tried_again(company, box):
    """R2-6: Dovecot's "[UNAVAILABLE] Temporary authentication failure" (RFC 5530: temporary) is retried at the next
    check, never a pause until the user acts."""
    box.imap_fail["login"] = imaplib.IMAP4.error(b"[UNAVAILABLE] Temporary authentication failure. [mx1:2026]")
    result = services.check_inbox(now=NOW)
    assert result["status"] == "failed" and result["kind"] == mailer.RETRY
    state = repo.mailbox_state(FROM, now=NOW)
    assert not state["paused"] and not state["auth_failed"]
    assert services.inbox_due(now=NOW + services.INBOX_EVERY) is True
    box.imap_fail.clear()
    assert services.check_inbox(now=NOW + services.INBOX_EVERY)["status"] == "ok"


def test_a_providers_block_keeps_its_own_reason_when_a_refused_login_is_fixed(company, box):
    """R2-11: after the login pause is lifted (Save), the provider's block still running shows its own reason, not the
    stale login error (which would invite a Try again on a suspended account)."""
    repo.record_email_failure(0, "", mailer.PROVIDER_BLOCK, "554 5.7.1 account suspended", FROM, now=NOW)
    repo.pause_mailbox_login(FROM, "Your mailbox refused the username or password for reading mail", now=NOW)
    state = repo.mailbox_state(FROM, now=NOW)
    assert state["login_refused"] and "refused the username" in state["pause_reason"]
    assert state["wait_reason"] == "554 5.7.1 account suspended"
    repo.retry_mailbox(FROM, now=NOW + timedelta(minutes=5), auth_only=True)
    state = repo.mailbox_state(FROM, now=NOW + timedelta(minutes=5))
    assert state["paused"] and state["pause_reason"] == "554 5.7.1 account suspended"


def test_a_rejection_after_data_is_not_recorded_as_sent(company, box):
    """R2UI-1: smtplib returns the server's final answer to DATA (it raises only when DATA can't start). A 554 spam
    refusal there is a provider block: not sent, the mailbox paused, no copy in the Sent folder; a 451 is retried."""
    turn_on(company)
    msg = approved(lead(company))
    box.fail["data"] = (554, b"5.7.1 Message rejected as spam")
    result = send(NOW)
    assert result["status"] == mailer.PROVIDER_BLOCK and box.sent == [] and len(box.refused) == 1
    stored = repo.get_message(msg.id)
    assert stored.status == "approved" and stored.sent_at is None and stored.email_state == "failed"
    assert repo.mailbox_state(FROM, now=NOW)["paused_until"] and box.appended == []
    other = approved(lead(company))
    repo.retry_mailbox(FROM, now=NOW)
    box.fail["data"] = (451, b"4.7.1 Try again later")
    assert send(NOW + timedelta(minutes=6))["status"] == mailer.RETRY
    assert repo.get_message(other.id).email_state == "" and box.sent == [] and box.appended == []


def test_the_test_email_refused_after_data_says_so(box):
    """R2UI-1: a refused test email isn't "Test email sent"."""
    box.fail["data"] = (554, b"5.7.1 Message rejected as spam")
    outcome = services.send_test_email()
    assert not outcome.ok and outcome.kind == mailer.PROVIDER_BLOCK


# --------------------------------------------------------------------------------------
# Never twice, whatever is deleted (F1, F2, WP5.1) and whatever address a lead has now (R2-3)
# --------------------------------------------------------------------------------------


def test_deleting_a_lead_never_frees_its_address_or_the_limits(company, box):
    """F1: Claude deletes the lead after its email went out; the same business is added again. Step 1 isn't emailed
    again, the daily limit and the minutes between two emails still count the deleted email."""
    turn_on(company, email_daily_limit=1)
    hotel = lead(company, "reservations@palm-hotel.example", full_name="", lead_company="Palm Hotel")
    approved(hotel)
    assert send(NOW)["status"] == "sent"
    assert mcp_server.delete_lead(hotel)["deleted"] is True
    again = lead(company, "reservations@palm-hotel.example", full_name="", lead_company="Palm Hotel")
    step1 = approved(again)
    queue = repo.email_queue(company.id, now=NOW + timedelta(minutes=1))
    assert queue["sent_last_24h"] == 1 and queue["blocked_reason"] == "daily_limit"
    assert "step 1 was already emailed to reservations@palm-hotel.example" in skip_reason(company, step1.id)
    assert send(NOW + timedelta(days=2))["status"] == "idle" and len(box.sent) == 1


def test_a_reply_or_opt_out_about_a_deleted_lead_is_still_read(company, box):
    """F1: after the lead is deleted, Lina's STOP that refers to our Message-ID still puts her address on the
    never-email list, so re-adding her never emails her again."""
    turn_on(company)
    lina, _ = lina_sent(company)
    repo.delete_lead(lina)
    deliver(box, "stop.eml")
    result = services.check_inbox(now=NOW + timedelta(minutes=5))
    assert result == {"status": "ok", "checked": 1, "recorded": 1}
    assert suppressed(LINA) == {LINA: "opt_out"}
    events = repo.list_email_events(company.id)
    assert events and events[0]["kind"] == "opt_out" and events[0]["lead_id"] is None
    again = approved(lead(company, LINA))
    assert "asked not to be emailed" in skip_reason(company, again.id)


def test_a_reply_about_a_deleted_lead_stops_its_address_through_any_lead(company, box):
    """F1: a plain reply that arrives after the lead was deleted still means the address answered."""
    turn_on(company)
    lina, _ = lina_sent(company)
    repo.delete_lead(lina)
    box.deliver(reply("Thanks, call me next week."))
    services.check_inbox(now=NOW + timedelta(minutes=5))
    again = approved(lead(company, LINA), step=2)
    assert skip_reason(company, again.id) == repo.ADDRESS_REPLIED_DELETED


def test_deleting_a_maybe_sent_email_keeps_it_counted(company, box):
    """F1: an email marked "May have been sent" deleted with the trash button still counts toward the daily limit,
    and its step is never sent again to the address."""
    turn_on(company, email_daily_limit=1)
    hotel = lead(company)
    msg = approved(hotel)
    set_row(msg.id, email_state="unknown", email_claimed_at=repo.iso(NOW), email_to=repo.get_lead(hotel).email,
            email_message_id="<maybe@acme-mail.example>")
    repo.delete_message(msg.id)
    rewritten = approved(hotel, body="A rewritten step 1")
    assert repo.email_queue(company.id, now=NOW + timedelta(minutes=6))["blocked_reason"] == "daily_limit"
    assert "already emailed" in skip_reason(company, rewritten.id, now=NOW + timedelta(days=2))


def test_deleting_a_company_keeps_the_never_twice_history(company, box):
    """WP5.1 / demo-D7: after company A emailed pat@, company B's lead with that address is still held back, the
    mailbox's 24-hour count is unchanged, and A's email events stay without a company."""
    turn_on(company)
    approved(lead(company, "pat@acme-real.example"))
    assert send(NOW)["status"] == "sent"
    b = turn_on(repo.create_company(CompanyIn(name="Second Co")))
    b_email = approved(lead(b, "pat@acme-real.example"))
    events_before = len(repo.list_email_events())
    repo.record_email_failure(0, "", mailer.FAILED, "noted", FROM)  # any event of A's company
    with db.connect() as c:
        c.execute("INSERT INTO email_events (company_id, kind, detail, created_at) VALUES (?, 'bounce', 'x', ?)",
                  (company.id, repo.iso(NOW)))
    count = repo.email_sending_status(b.id, now=NOW + timedelta(minutes=10))["mailbox_sent_24h"]
    repo.delete_company(company.id)
    assert "already emailed to pat@acme-real.example" in skip_reason(b, b_email.id, now=NOW + timedelta(minutes=10))
    assert repo.email_sending_status(b.id, now=NOW + timedelta(minutes=10))["mailbox_sent_24h"] == count == 1
    kept = repo.list_email_events()
    assert len(kept) >= events_before + 1 and all(e["company_id"] is None for e in kept)


@pytest.mark.parametrize("what", ["lead", "company"])
def test_nothing_is_deleted_while_its_email_is_being_sent(company, box, what):
    """F2: deleting the lead (or company) while its email is in DATA would leave it sent and unrecorded."""
    turn_on(company)
    hotel = lead(company)
    msg = approved(hotel)
    set_row(msg.id, email_state="data", email_claim="c", email_claimed_at=repo.iso(NOW))
    with pytest.raises(ValueError, match="being sent right now"):
        repo.delete_lead(hotel) if what == "lead" else repo.delete_company(company.id)
    if what == "lead":
        with pytest.raises(mcp_server.ToolError, match="being sent right now"):
            mcp_server.delete_lead(hotel)
    assert repo.get_message(msg.id).email_state == "data"


def test_an_address_that_replied_is_never_emailed_through_another_lead_after_its_lead_changed(company, box):
    """R2-3: account lead B (reservations@) was emailed and replied; B then gets the events manager's address. Lead A,
    another branch with reservations@, stays held back, and step 1 never goes to reservations@ twice."""
    turn_on(company)
    branch_b = lead(company, "reservations@palmhotel.example", full_name="", lead_company="Palm Hotel Marina")
    sent_from_mailbox(branch_b)
    services.check_inbox(now=NOW)
    box.deliver(reply("Please write to our events manager at events@palmhotel.example",
                      sender="reservations@palmhotel.example", name="Palm Hotel"))
    services.check_inbox(now=NOW + timedelta(minutes=5))
    branch_a = lead(company, "reservations@palmhotel.example", full_name="", lead_company="Palm Hotel JBR")
    a_email = approved(branch_a)
    repo.update_lead(branch_b, {"email": "events@palmhotel.example"})
    assert skip_reason(company, a_email.id, now=NOW + timedelta(days=5)) == repo.ADDRESS_REPLIED
    with db.connect() as c:  # the claim's own check, should the queue's be bypassed
        c.execute("DELETE FROM messages WHERE direction = 'inbound'")
    assert "already emailed to reservations@palmhotel.example" in skip_reason(company, a_email.id,
                                                                              now=NOW + timedelta(days=5))
    with db.connect() as c:
        assert repo._claim_email(c, repo.get_company(company.id, conn=c), a_email.id, "x", FROM, "",
                                 NOW + timedelta(days=5)) is None


# --------------------------------------------------------------------------------------
# Leads, messages and settings that only the user may loosen (R2-7/F4, F7, F8, F10, R2-8/R2UI-4, R2-13, R2UI-11)
# --------------------------------------------------------------------------------------


def test_claude_or_the_api_never_drop_the_opt_out_tag(company, box):
    """R2-7, F4: an opted-out lead given new tags and a new address by Claude (or a CRM sync over the API) keeps its
    "unsubscribed" tag and is never emailed. Only the user's own lead form can remove it."""
    turn_on(company)
    lina = lead(company, LINA, full_name="Lina Noor")
    repo.record_opt_out(lina, "email reply", "Opt-out: stop", automatic=True)
    out = mcp_server.update_lead(lina, {"tags": ["dmc", "vip"], "email": "lina@emaar.example", "status": "qualified"})
    assert "unsubscribed" in repo.get_lead(lina).tags and "kept" in out["tags_note"]
    fresh = approved(lina)
    assert skip_reason(company, fresh.id) == repo.UNSUBSCRIBED
    repo.update_lead(lina, {"tags": ["crm-sync"]})  # the API's PATCH uses the same default
    assert "unsubscribed" in repo.get_lead(lina).tags
    repo.update_lead(lina, {"tags": ["vip"]}, keep_opt_out=False)  # the user's decision, on the lead's page
    assert repo.get_lead(lina).tags == ["vip"]


def test_marking_a_message_replied_takes_back_the_approved_emails(company, box):
    """F7: Claude marks step 1 replied; the approved step 2 goes back to draft, so setting the lead back to Contacted
    later never sends an email approved before the reply."""
    turn_on(company)
    lina = lead(company, LINA)
    step1 = sent_from_mailbox(lina, when=NOW - timedelta(days=10))
    step2 = approved(lina, step=2)
    mcp_server.update_message(step1.id, status="replied")
    assert repo.get_lead(lina).status == "replied" and repo.get_message(step2.id).status == "draft"
    mcp_server.update_lead(lina, {"status": "contacted"})
    assert send(NOW)["status"] == "idle" and box.sent == []


def test_follow_up_waits_are_at_least_a_day(company, box):
    """F8: followup_days [0, -3] from Claude would let three steps go out within ten minutes."""
    out = mcp_server.update_company(company.id, {"outreach": {"followup_days": [0, -3, 500]}})
    assert out["profile"]["outreach"]["followup_days"] == [1, 1, 90]
    turn_on(company)
    lina = lead(company, LINA)
    for step in (1, 2, 3):
        approved(lina, step=step)
    assert send(NOW)["status"] == "sent"
    assert send(NOW + timedelta(minutes=5))["status"] == "idle"
    assert send(NOW + timedelta(minutes=10))["status"] == "idle" and len(box.sent) == 1


def test_a_turn_off_clicked_while_claude_saves_the_profile_stays_off(company, box, monkeypatch):
    """F10: Claude's update_company checks the email settings against the profile it read; the user's Turn off lands
    between that check and the write. The write checks again against the stored profile, so sending stays off."""
    turn_on(company)
    real_update = repo.update_company

    def user_turns_off_first(company_id, changes, conn=None, guard=None):
        real_update(company_id, {"outreach": {"email_sending": False}})  # the dashboard, a moment earlier
        return real_update(company_id, changes, conn=conn, guard=guard)

    monkeypatch.setattr(repo, "update_company", user_turns_off_first)
    with pytest.raises(mcp_server.ToolError, match="turn email sending on"):
        mcp_server.update_company(company.id, {"outreach": {"tone": "warm", "email_sending": True}})
    monkeypatch.setattr(repo, "update_company", real_update)
    assert repo.get_company(company.id).outreach.email_sending is False


def test_fixing_a_bounced_address_and_approving_again_queues_the_email(company, box):
    """R2-8, R2UI-4: a bounce at RCPT leaves the email failed. Once the address is fixed and the email approved again,
    it is queued (no "Fix the address" loop), and the queue says nothing about the old failure."""
    turn_on(company)
    old = lead(company, "sales@oldcreek.example", full_name="Hana")
    msg = approved(old)
    box.fail["rcpt"] = (550, b"5.1.1 <sales@oldcreek.example>: Recipient address rejected: User unknown")
    assert send(NOW)["status"] == mailer.BOUNCE
    assert repo.get_message(msg.id).email_state == "failed"
    box.fail.clear()
    repo.update_lead(old, {"email": "reservations@oldcreek.example"})
    stored = repo.get_message(msg.id)
    assert (stored.status, stored.email_state, stored.send_error) == ("draft", "", "")
    repo.update_message(msg.id, status="approved")
    assert [i["message_id"] for i in repo.email_queue(company.id, now=NOW + timedelta(minutes=6))["items"]] == [msg.id]
    assert send(NOW + timedelta(minutes=6))["status"] == "sent" and box.sent[-1]["to"] == "reservations@oldcreek.example"


def test_something_coming_back_for_a_maybe_sent_draft_makes_it_sent(company, box):
    """R2UI-11: an email that may have been sent and was set back to draft is marked sent when a reply refers to it,
    never left as "Draft | Sent from your mailbox"."""
    turn_on(company)
    lina = lead(company, LINA, full_name="Lina Noor")
    msg = approved(lina)
    set_row(msg.id, status="draft", email_state="unknown", email_message_id=OUR_ID, email_to=LINA,
            email_claimed_at=repo.iso(NOW))
    services.check_inbox(now=NOW)
    box.deliver(eml("ooo_autosubmitted.eml", lead=LINA, ours=OUR_ID))
    services.check_inbox(now=NOW + timedelta(minutes=5))
    stored = repo.get_message(msg.id)
    assert (stored.status, stored.email_state) == ("sent", "sent")


@pytest.mark.parametrize("sender,answers_it", [(LINA, True), (LINA, False), ("lina.noor@gmail.com", True)])
def test_a_reply_after_send_again_is_read_and_the_email_is_not_sent_twice(company, box, sender, answers_it):
    """EMAIL-3: an email may have been sent (the connection broke during DATA). At 19:00 in Dubai the user chooses Send
    again: it waits for the sending hours. That evening Lina answers the first copy, which did arrive, or writes a new
    email from her phone. It is read, and nothing goes out in the morning. A reply to the first copy (Send again keeps
    its Message-ID) also records the email as sent. So does one from her own Gmail, found only by that Message-ID."""
    turn_on(company)
    lina = lead(company, LINA, full_name="Lina Noor")
    msg = approved(lina)
    box.fail["data"] = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    assert send(NOW)["status"] == mailer.UNKNOWN
    del box.fail["data"]
    header = repo.get_message(msg.id).email_message_id
    evening = NOW + timedelta(hours=9)  # 19:00 in Dubai: outside the sending hours
    services.check_inbox(now=evening)
    repo.resolve_email(msg.id, "send_again", now=evening)
    box.deliver(reply("Thanks Sam, please call me tomorrow instead.", sender=sender, refers=header if answers_it else ""))
    assert services.check_inbox(now=evening + timedelta(minutes=5))["recorded"] == 1
    assert repo.get_lead(lina).status == "replied"
    stored = repo.get_message(msg.id)
    if answers_it:
        assert (stored.status, stored.email_state, stored.sent_at) == ("sent", "sent", NOW)
    assert send(NOW + timedelta(days=1)) == {"status": "idle"} and box.sent == []


@pytest.mark.parametrize("how", ["provider_block", "retries"])
def test_a_re_send_that_fails_after_send_again_keeps_the_lead_read(company, box, how):
    """P2-INBOX-1: an email may have been sent, and the user chose Send again. The re-send fails for good: the
    provider's daily quota, or 5 tries while the server can't be reached. Lina got the first copy, so her address and
    her company's domain stay in the inbox check, also while the re-send is under way. Her new email and her PA's note
    are read, and Try again sends nothing."""
    lina, msg = maybe_sent(company, box)
    evening = NOW + timedelta(hours=9)
    services.check_inbox(now=evening)
    repo.resolve_email(msg.id, "send_again", now=evening)
    for state in ("claimed", "data"):
        set_row(msg.id, email_state=state)
        assert LINA in repo.inbound_index(now=evening)["leads"]
    set_row(msg.id, email_state="")
    when = NOW + timedelta(days=1)
    if how == "provider_block":
        re_send_blocked(box, when)
    else:
        box.fail["connect"] = ConnectionRefusedError("refused")
        for _ in range(repo.EMAIL_MAX_ATTEMPTS):
            assert send(when)["status"] == mailer.RETRY
            when += timedelta(minutes=90)
        box.fail.clear()
    assert repo.get_message(msg.id).email_state == "failed"
    index = repo.inbound_index(now=when)
    assert LINA in index["leads"] and "gulfbank.example" in index["domains"]
    box.deliver(reply("Hi Sam, got your note yesterday. Call me at 3pm.", sender=LINA, refers="",
                      subject="Airport transfers", msg_id="lina-new"))
    box.deliver(reply("Lina is travelling this week.", sender="pa@gulfbank.example", refers="", name="Huda",
                      msg_id="pa-note"))
    assert services.check_inbox(now=when + timedelta(minutes=5))["recorded"] == 2
    assert repo.get_lead(lina).status == "replied"
    later = when + timedelta(days=1)
    repo.resolve_email(msg.id, "retry", now=later)
    assert send(later) == {"status": "idle"} and box.sent == []


@pytest.mark.parametrize("then", ["nothing", "send_again", "re_send_failed"])
def test_an_auto_reply_to_a_maybe_sent_email_records_it_as_any_sent_email(company, box, then):
    """P2-INBOX-3: an email may have been sent (also after Send again, and after a re-send that failed). Lina's
    out-of-office answers it, so it went out. It is recorded as a sent email is: sent when it went out, from the
    mailbox, and Lina is contacted, so step 2 comes due 3 days after it."""
    lina, msg = maybe_sent(company, box, followup_days=[3])
    when = NOW + timedelta(hours=9)
    services.check_inbox(now=when)
    if then != "nothing":
        repo.resolve_email(msg.id, "send_again", now=when)
    if then == "re_send_failed":
        when = NOW + timedelta(days=1)
        re_send_blocked(box, when)
    box.deliver(eml("ooo_autosubmitted.eml", lead=LINA, ours=msg.email_message_id))
    assert services.check_inbox(now=when + timedelta(minutes=5))["recorded"] == 1
    stored = repo.get_message(msg.id)
    assert (stored.status, stored.email_state, stored.sent_via, stored.sent_at) == ("sent", "sent", "smtp", NOW)
    assert repo.get_lead(lina).status == "contacted"
    assert repo.company_stats(company.id)["statuses"]["contacted"] == 1
    assert repo.followups_due(company.id, now=NOW + timedelta(days=3) - timedelta(minutes=1)) == []
    assert [d["lead"].id for d in repo.followups_due(company.id, now=NOW + timedelta(days=3))] == [lina]


@pytest.mark.parametrize("then", ["edited", "edited_after_a_failed_re_send", "back_to_drafts"])
def test_an_email_edited_after_send_again_is_never_recorded_sent_with_text_the_lead_never_got(company, box, then):
    """P2-INBOX-4: an email may have been sent, the user chose Send again, then changed its text (also once a re-send
    failed). Lina answers the first copy: her reply is logged and stops the email, which stays unsent: she never got
    that text. The lead page says why. Approved again, it never goes, by the queue or the claim's own check. Set back
    to draft without a change of text, it is recorded as sent."""
    lina, msg = maybe_sent(company, box)
    when = NOW + timedelta(hours=9)
    services.check_inbox(now=when)
    repo.resolve_email(msg.id, "send_again", now=when)
    if then == "edited_after_a_failed_re_send":
        when = NOW + timedelta(days=1)
        re_send_blocked(box, when)
    if then == "back_to_drafts":
        repo.update_message(msg.id, status="draft")
    else:
        assert repo.update_message(msg.id, body="Completely new text the lead never got.").status == "draft"
    box.deliver(reply("Thanks Sam, call me.", sender=LINA, refers=msg.email_message_id, msg_id="lina-answer"))
    assert services.check_inbox(now=when + timedelta(minutes=5))["recorded"] == 1
    assert repo.get_lead(lina).status == "replied"
    stored = repo.get_message(msg.id)
    if then == "back_to_drafts":
        assert (stored.status, stored.email_state, stored.sent_at, stored.body) == ("sent", "sent", NOW, BODY)
        return
    assert (stored.status, stored.email_state, stored.sent_at) == ("skipped", "", None)
    edits = repo.list_email_events(lead_id=lina, kinds=("edited",))  # on the lead page
    assert [e["detail"] for e in edits] == [repo.EDITED_AFTER_MAYBE_SENT]
    later = NOW + timedelta(days=2)
    repo.update_message(msg.id, status="approved")
    assert skip_reason(company, msg.id, now=later) == "the lead is marked Replied"
    assert send(later) == {"status": "idle"} and box.sent == []
    with db.connect() as c:  # the claim's own check, should the queue's be bypassed
        assert repo._claim_email(c, repo.get_company(company.id, conn=c), msg.id, "x", FROM, "", later) is None
    # Nor a new step 1 in its place, nor one through a new lead once Lina's is deleted.
    repo.delete_message(msg.id)
    approved(lina)
    assert send(later) == {"status": "idle"}
    repo.delete_lead(lina)
    approved(lead(company, LINA))
    assert send(later + timedelta(hours=1)) == {"status": "idle"} and box.sent == []


# --------------------------------------------------------------------------------------
# The inbox: backlog, clock, colleagues, maybe-sent emails (F3, F5, F9, INBOX2-2, R2UI-12b)
# --------------------------------------------------------------------------------------


def test_a_backlog_is_read_before_anything_is_sent(company, box, monkeypatch):
    """F3: 250 unrelated emails arrived overnight, then Lina's STOP. The morning check reads them all before step 2
    can go; a backlog too big for one check counts as not checked, so nothing is sent."""
    turn_on(company)
    lina, _ = lina_sent(company)
    step2 = approved(lina, step=2)
    set_row(step2.id, created_at=repo.iso(NOW - timedelta(days=1)))
    for _ in range(250):
        box.deliver(eml("unrelated.eml"))
    deliver(box, "stop.eml")
    result = services.check_inbox(now=NOW + timedelta(days=10))
    assert result["status"] == "ok" and result["recorded"] == 1
    assert repo.get_lead(lina).status == "disqualified"
    assert services.send_next_email(now=NOW + timedelta(days=10)) == {"status": "idle"} and box.sent == []
    monkeypatch.setattr(services, "CATCH_UP_ROUNDS", 1)
    for _ in range(250):
        box.deliver(eml("unrelated.eml"))
    result = services.check_inbox(now=NOW + timedelta(days=10, minutes=5))
    assert result["status"] == "failed" and "still reading the new mail in INBOX" in result["text"]
    assert not repo.mailbox_state(FROM, now=NOW + timedelta(days=10, minutes=5))["inbox_ok"]
    assert services.check_inbox(now=NOW + timedelta(days=10, minutes=10))["status"] == "ok"  # it goes on


def test_a_clock_that_went_back_neither_counts_as_a_fresh_check_nor_stops_the_checks(company, box):
    """F5, WP5.2: an inbox check stamped 6 hours ahead (the clock was wrong, then corrected) isn't fresh, and the
    automatic check runs at once instead of in 6 hours."""
    turn_on(company)
    lina, _ = lina_sent(company)
    approved(lina, step=2)
    ahead = NOW + timedelta(hours=6)
    repo.save_inbox_check(FROM, True, now=ahead)
    assert repo.mailbox_state(FROM, now=NOW)["inbox_ok"] is False
    assert services.inbox_due(now=NOW) is True
    assert services.send_next_email(now=NOW + timedelta(hours=2)) == {"status": "idle"} and box.sent == []


def test_a_reply_without_references_to_a_maybe_sent_email_is_read(company, box):
    """F9: step 1 may have been sent; Lina writes a new email from her phone (no In-Reply-To). It is read."""
    turn_on(company)
    lina = lead(company, LINA, full_name="Lina Noor")
    msg = approved(lina)
    set_row(msg.id, email_state="unknown", email_claimed_at=repo.iso(NOW - timedelta(hours=1)), email_to=LINA,
            email_message_id=OUR_ID)
    services.check_inbox(now=NOW)
    box.deliver(reply("Hi Sam, what are your rates for 3 cars?", refers="", subject="Cars"))
    assert services.check_inbox(now=NOW + timedelta(minutes=5))["recorded"] == 1
    assert repo.get_lead(lina).status == "replied"


def test_a_colleagues_forward_is_read_and_says_who_wrote(company, box):
    """INBOX2-2: the GM's assistant forwards our email from her own address (Outlook: no References) asking to stop.
    It is matched to the lead emailed at that company, recorded with who wrote it, stops the follow-up, and the
    opt-out, in someone else's words, waits for the user."""
    item = CORPUS_ITEMS["52_optout_pa_forward_no_references.eml"]
    turn_on(company)
    omar = lead(company, item["lead"], full_name="Omar")
    sent_from_mailbox(omar, header_id=item["our_id"])
    follow_up = approved(omar, step=2)
    services.check_inbox(now=NOW)
    box.deliver((CORPUS / item["file"]).read_bytes())
    assert services.check_inbox(now=NOW + timedelta(minutes=5))["recorded"] == 1
    stored = next(m for m in repo.list_messages(company.id, lead_id=omar) if m.direction == "inbound")
    assert stored.email_from == "pa@desert-pearl-hotel.example" and "remove him" in stored.opt_out_hint
    assert repo.get_message(follow_up.id).status == "skipped" and suppressed(item["lead"]) == {}
    # The same domain's newsletter or auto-reply is not about our email: not stored.
    box.deliver(eml("ooo_autosubmitted.eml", lead="concierge@desert-pearl-hotel.example", ours="<none@x>"))
    services.check_inbox(now=NOW + timedelta(minutes=10))
    assert [e["kind"] for e in repo.list_email_events(lead_id=omar)] == ["reply"]


def test_a_colleagues_forward_goes_to_the_person_it_names(company, box):
    """INBOX2-2, two people at the same hotel got the same email: the PA's forward about Omar quotes "To: omar...". It
    is logged on Omar, not on Rania (emailed later), and Omar gets no follow-up."""
    item = CORPUS_ITEMS["52_optout_pa_forward_no_references.eml"]
    turn_on(company)
    omar = lead(company, item["lead"], full_name="Omar")
    sent_from_mailbox(omar, header_id=item["our_id"], when=NOW - timedelta(hours=3))
    rania = lead(company, "rania.fom@desert-pearl-hotel.example", full_name="Rania")
    sent_from_mailbox(rania, header_id="<rania-1@acme-mail.example>", when=NOW - timedelta(hours=1))
    omar_follow_up, rania_follow_up = approved(omar, step=2), approved(rania, step=2)
    services.check_inbox(now=NOW)
    box.deliver((CORPUS / item["file"]).read_bytes())
    assert services.check_inbox(now=NOW + timedelta(minutes=5))["recorded"] == 1
    assert [m.direction for m in repo.list_messages(company.id, lead_id=omar)].count("inbound") == 1
    assert [m.direction for m in repo.list_messages(company.id, lead_id=rania)].count("inbound") == 0
    assert repo.get_message(omar_follow_up.id).status == "skipped"
    assert repo.get_message(rania_follow_up.id).status == "approved"  # nobody asked about Rania
    # EMAIL-2: only Omar is flagged and stopped for good. Rania's follow-up goes out when it is due.
    later = NOW + timedelta(days=7)
    assert [m.lead_id for m in repo.opt_out_hints(company.id)] == [omar]
    assert send(later)["message_id"] == rania_follow_up.id
    again = approved(omar, step=2)
    assert repo.followups_due(company.id, now=later) == []
    assert skip_reason(company, again.id, now=later) == "the lead is marked Replied"


@pytest.mark.parametrize("note,hint", [
    ("Please don't email our GM again, he isn't interested.", ""),
    ("Our GM asked me to tell you: please remove him from your list.", "remove him from your list"),
])
def test_a_colleagues_note_naming_nobody_stops_everyone_it_may_be_about(company, box, note, hint):
    """INBOX2-2, EMAIL-2: a colleague writes about "our GM" without naming anyone, and two people there got that email:
    it is logged on the latest and noted on the other too, so the right one can't keep getting emails. A later step to
    either is never due, auto-approved or sent, also by the AI agent. A note that looks like an opt-out is flagged on
    each, so the user says which lead it is about. Only the latest moves to Replied, so the reply rate counts it once."""
    turn_on(company, email_send_auto_approved=True, auto_approve=True, auto_approve_hours=24)
    first = lead(company, "omar.gm@desert-pearl-hotel.example", full_name="Omar")
    sent_from_mailbox(first, header_id="<omar-1@acme-mail.example>", when=NOW - timedelta(hours=3))
    second = lead(company, "rania.fom@desert-pearl-hotel.example", full_name="Rania")
    sent_from_mailbox(second, header_id="<rania-1@acme-mail.example>", when=NOW - timedelta(hours=1))
    follow_ups = [approved(first, step=2), approved(second, step=2)]
    services.check_inbox(now=NOW)
    box.deliver(reply(note, sender="pa@desert-pearl-hotel.example", subject="RE: Airport transfers for your guests",
                      refers="", name="Noura"))
    result = services.check_inbox(now=NOW + timedelta(minutes=5))
    assert result["recorded"] == 1
    assert [repo.get_message(f.id).status for f in follow_ups] == ["skipped", "skipped"]
    assert [m.direction for m in repo.list_messages(company.id, lead_id=second)].count("inbound") == 1
    stopped = [e for e in repo.list_email_events(lead_id=first) if e["detail"].startswith("Follow-ups stopped")]
    assert stopped and "pa@desert-pearl-hotel.example" in stopped[0]["detail"]
    # EMAIL-2: the note is on both pages, from the PA, flagged the same way. Only Rania (the latest) is Replied.
    notes = repo.list_messages(company.id, direction="inbound")
    assert sorted(m.lead_id for m in notes) == sorted([first, second])
    assert {(m.email_from, m.opt_out_hint) for m in notes} == {("pa@desert-pearl-hotel.example", hint)}
    assert len(repo.opt_out_hints(company.id)) == (2 if hint else 0)
    assert [repo.get_lead(first).status, repo.get_lead(second).status] == ["contacted", "replied"]
    assert repo.company_stats(company.id)["statuses"]["replied"] == 1
    # Omar's next step is never due. Written anyway, auto-approve leaves it, and approved by hand it isn't sent.
    later = NOW + timedelta(days=7)
    assert repo.followups_due(company.id, now=later) == []
    draft = approved(first, step=2, status="draft")
    set_row(draft.id, created_at=repo.iso(later - timedelta(days=2)), updated_at=repo.iso(later - timedelta(days=2)))
    assert repo.auto_approve_due(company.id, now=later)["approved"] == []
    assert repo.get_message(draft.id).status == "draft"
    by_hand = approved(first, step=2)
    assert skip_reason(company, by_hand.id, now=later) == repo.LEAD_REPLIED
    assert send(later) == {"status": "idle"} and box.sent == []
    with db.connect() as c:  # the claim's own check, should the queue's be bypassed
        assert repo._claim_email(c, repo.get_company(company.id, conn=c), by_hand.id, "x", FROM, "", later) is None
    # Nor does the AI agent send him a LinkedIn message.
    repo.update_company(company.id, {"outreach": {"agent_sending": True}})
    repo.update_lead(first, {"linkedin_url": "https://www.linkedin.com/in/omar-gm"})
    dm = repo.create_message(first, "Hi Omar, following up.", channel="linkedin_dm", step=2, status="approved",
                             generated_by="claude")
    skipped = {s["message_id"]: s["reason"] for s in repo.send_queue(company.id, now=later)["skipped"]}
    assert skipped[dm.id] == repo.LEAD_REPLIED


def test_mail_from_a_free_mail_domain_is_no_colleague(company, box):
    """INBOX2-2: two people with Gmail addresses aren't colleagues."""
    turn_on(company)
    pat = lead(company, "pat.travel@gmail.com")
    sent_from_mailbox(pat)
    services.check_inbox(now=NOW)
    box.deliver(reply("Unsubscribe", sender="someone.else@gmail.com", refers=""))
    assert services.check_inbox(now=NOW + timedelta(minutes=5)) == {"status": "ok", "checked": 0, "recorded": 0}


def test_the_mail_apps_unsubscribe_button_is_an_event_not_a_reply(company, box):
    """R2UI-12b: Gmail's Unsubscribe button sends "This message was automatically generated by Gmail": the address is
    suppressed and the lead tagged, without a fake reply in the Replies tab."""
    turn_on(company)
    lina, _ = lina_sent(company)
    deliver(box, "unsubscribe_list.eml")
    services.check_inbox(now=NOW + timedelta(minutes=5))
    assert suppressed(LINA) == {LINA: "opt_out"} and "unsubscribed" in repo.get_lead(lina).tags
    assert [m for m in repo.list_messages(company.id, lead_id=lina) if m.direction == "inbound"] == []
    assert repo.list_email_events(lead_id=lina)[0]["detail"] == repo.UNSUBSCRIBE_BUTTON


# --------------------------------------------------------------------------------------
# Opt-outs: only an unambiguous one acts; one that only looks like it is flagged for the user
# (R2-4, R2-9, INBOX2-1, INBOX2-3, R2UI-2, R2UI-9)
# --------------------------------------------------------------------------------------


def test_a_reply_that_only_looks_like_an_opt_out_is_flagged_and_the_user_decides(company, box):
    """The decision: a booking reply with "don't contact me" words is a reply flagged "Looks like an opt-out": it
    stopped the sequence, nothing is suppressed. Never email again does the full opt-out; Not an opt-out clears it."""
    turn_on(company)
    lina, _ = lina_sent(company)
    box.deliver(reply("Who gave you my email? Please remove me from your list, and send the rates to our office."))
    services.check_inbox(now=NOW + timedelta(minutes=5))
    flagged = repo.opt_out_hints(company.id)
    assert [m.lead_id for m in flagged] == [lina] and "remove me from your list" in flagged[0].opt_out_hint
    assert suppressed(LINA) == {} and repo.get_lead(lina).status == "replied"
    assert repo.email_attention(company.id)["hints"][0].id == flagged[0].id
    status = mcp_server.get_email_status(company.id)
    assert status["needs_attention"]["looks_like_opt_out"][0]["message_id"] == flagged[0].id
    assert "looks like an opt-out" in mcp_server.get_lead(lina)["looks_like_opt_out"]
    repo.clear_opt_out_hint(flagged[0].id)
    assert repo.opt_out_hints(company.id) == [] and suppressed(LINA) == {}
    box.deliver(reply("Stop. We already booked with another company.", msg_id="second"))
    services.check_inbox(now=NOW + timedelta(minutes=10))
    second = repo.opt_out_hints(company.id)[0]
    repo.confirm_opt_out_hint(second.id)
    assert suppressed(LINA) == {LINA: "opt_out"} and repo.get_lead(lina).status == "disqualified"
    assert repo.opt_out_hints(company.id) == [] and "unsubscribed" in repo.get_lead(lina).tags


@pytest.mark.parametrize("item", [i for i in MANIFEST["messages"] if i["expect"] in ("reply", "opt_out")],
                         ids=lambda i: i["file"][:2])
def test_the_corpus_through_the_inbox_check(company, box, item):
    """Every reply and opt-out of the reviewers' corpus, through the real inbox check: a booking reply is never an
    opt-out; a real opt-out is either acted on (the address suppressed) or flagged for the user, never missed; what
    the person wrote is kept, our quoted email is not."""
    turn_on(company)
    kind = item["lead_kind"]
    data = {"email": item["lead"], "lead_company": item["lead"].split("@")[1]}
    if kind == "person":
        data["full_name"] = item["lead_name"] or "Omar Haddad"
    found = repo.upsert_lead(company.id, LeadIn(**data))[0].id
    sent = sent_from_mailbox(found, header_id=item["our_id"])
    set_row(sent.id, email_to=item["sent_to"].lower())
    services.check_inbox(now=NOW)
    box.deliver((CORPUS / item["file"]).read_bytes(), item["folder"] if item["folder"] in box.folders else "INBOX")
    assert services.check_inbox(now=NOW + timedelta(minutes=5))["recorded"] == 1, item["file"]
    lead_now = repo.get_lead(found)
    blocked = suppressed(item["lead"], item["sent_to"])
    hints = repo.opt_out_hints(company.id)
    if item["expect"] == "reply":
        assert blocked == {} and "unsubscribed" not in lead_now.tags, item["file"]
    else:
        assert blocked or hints, f"{item['file']}: a real opt-out was missed"
    replies = [m for m in repo.list_messages(company.id, lead_id=found) if m.direction == "inbound"]
    stored = replies[0].body if replies else ""
    for keep in item["keep"]:
        assert keep in stored, f"{item['file']}: lost {keep!r}"
    for drop in item["drop"]:
        assert drop not in stored, f"{item['file']}: kept our {drop!r}"


@pytest.mark.parametrize("name", ["68_bounce_exim_unrouteable_domain.eml", "77_dsn_gateway_status_5_0_0_recipnotfound.eml",
                                  "80_dsn_yahoo_user_doesnt_have_account.eml"])
def test_hard_bounces_with_a_non_specific_status_suppress_the_address(company, box, name):
    """INBOX2-6: "Status: 5.0.0" with "550 5.1.1 RecipNotFound", Exim's "Unrouteable address" and Yahoo's "doesn't
    have a yahoo.com account" are hard bounces: the address is suppressed and the follow-up isn't sent."""
    item = CORPUS_ITEMS[name]
    turn_on(company)
    found = lead(company, item["lead"])
    sent = sent_from_mailbox(found, header_id=item["our_id"])
    set_row(sent.id, email_to=item["sent_to"].lower())
    services.check_inbox(now=NOW)
    box.deliver((CORPUS / name).read_bytes())
    services.check_inbox(now=NOW + timedelta(minutes=5))
    assert suppressed(item["sent_to"].lower()) == {item["sent_to"].lower(): "bounce"}
    assert repo.get_message(sent.id).email_state == "bounced"


def test_corpus_auto_replies_and_html_tables(company, box):
    """INBOX2-8, INBOX2-9: an Arabic out-of-office subject without RFC 3834 headers is an automatic reply; an HTML
    table keeps its cells apart; INBOX2-4 and INBOX2-7: Zoho's and Russian Outlook's quotes are cut, an Arabic line
    about books and a booking form's underscores are not."""
    ooo = mailer.classify_inbound((CORPUS / "56_ooo_arabic_subject_no_auto_headers.eml").read_bytes())
    assert ooo.kind == mailer.AUTO_REPLY
    table = mailer.classify_inbound((CORPUS / "08_html_only_table_booking.eml").read_bytes())
    assert "Flight BA 107" in table.text and "Guest Mr. John Carter" in table.text
    for name in ("04_zoho_reply_unprefixed_quote.eml", "05_zoho_reply_quoting_old_footer.eml",
                 "86_outlook_russian_reply.eml", "87_outlook_russian_reply_old_footer.eml"):
        inbound = mailer.classify_inbound((CORPUS / name).read_bytes(), [MANIFEST["footer"]])
        assert inbound.kind == mailer.REPLY and not inbound.opt_out_hint, name
        assert "We drive guests" not in inbound.text and "unsubscribe" not in inbound.text.lower(), name
    books = mailer.classify_inbound((CORPUS / "21_reply_arabic_books_line_ending_colon.eml").read_bytes())
    assert "3 فانات" in books.text and "5 نوفمبر" in books.text
    form = mailer.classify_inbound((CORPUS / "22_reply_underscore_separated_booking.eml").read_bytes())
    assert "LH 630" in form.text and "Bulgari Resort" in form.text
    assert mailer.strip_quoted("Ok\nDe: Sam <sam@acme-mail.example>\nEnviado el: lunes\nPara: Ana\nAsunto: Cars\nOld"
                               ) == "Ok"
    assert mailer.strip_quoted("Ok\nDa: Sam <sam@acme-mail.example>\nInviato: lunedì\nA: Ana\nOggetto: Cars\nOld") == "Ok"


# --------------------------------------------------------------------------------------
# Schema version 6
# --------------------------------------------------------------------------------------


def test_a_version_5_database_keeps_its_email_events_when_a_company_is_deleted(settings):
    """WP5.1: version 5's email_events cascade with their company; version 6 keeps them (company_id NULL), adds the
    sent-email log and the new columns, and passes integrity_check."""
    v5 = db.SCHEMA.replace("REFERENCES companies(id) ON DELETE SET NULL,  -- kept when the company is deleted (version 6)",
                           "REFERENCES companies(id) ON DELETE CASCADE,")
    v5 = v5.split("-- Version 6. Emails that went out")[0]
    v5 = "\n".join(line for line in v5.split("\n") if not line.strip().startswith(("email_from", "opt_out_hint",
                                                                                   "alert_failed_at", "until_reason")))
    v5 = v5.replace("send_error TEXT NOT NULL DEFAULT '',  -- why sending failed, in plain words\n);",
                    "send_error TEXT NOT NULL DEFAULT ''\n);").replace("updated_at TEXT NOT NULL,\n);",
                                                                        "updated_at TEXT NOT NULL\n);")
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(v5)
    now = repo.iso()
    conn.execute("INSERT INTO companies (name, created_at, updated_at) VALUES ('Old Co', ?, ?)", (now, now))
    conn.execute("INSERT INTO email_events (company_id, kind, detail, created_at) VALUES (1, 'bounce', 'x', ?)", (now,))
    conn.execute("INSERT INTO email_mailbox (address, paused_until, pause_reason, updated_at) VALUES (?, ?, ?, ?)",
                 (FROM, repo.iso(NOW + timedelta(days=400)), "554 blocked", now))
    conn.execute("PRAGMA user_version=5")
    conn.commit()
    assert "email_from" not in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    conn.close()
    db.reset_init_cache()
    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 6
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert {"email_from", "opt_out_hint"} <= {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        assert "email_sent_log" in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert c.execute("SELECT until_reason FROM email_mailbox").fetchone()[0] == "554 blocked"
    repo.delete_company(1)
    events = repo.list_email_events()
    assert len(events) == 1 and events[0]["company_id"] is None


# --------------------------------------------------------------------------------------
# The time zone of the sending hours (F6)
# --------------------------------------------------------------------------------------


def test_the_card_names_the_time_zone_of_the_sending_hours(monkeypatch):
    """F6: in Docker the sending hours run on the server's clock (UTC by default): the card says which one."""
    from openberry.web import ui

    assert ui.clock_zone(NOW) == "UTC+04:00"
    monkeypatch.setattr(repo, "EMAIL_LOCAL_TZ", timezone.utc)
    assert ui.clock_zone(NOW) == "UTC"
    monkeypatch.setattr(repo, "EMAIL_LOCAL_TZ", timezone(-timedelta(hours=5, minutes=30)))
    assert ui.clock_zone(NOW) == "UTC-05:30"


def test_tz_in_the_settings_file_sets_the_clock(tmp_path, monkeypatch):
    """F6: TZ=Asia/Dubai in OpenBerry's .env file (outside Docker too) changes the clock of the sending hours."""
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("POSIX time zones only")
    monkeypatch.delenv("TZ", raising=False)
    env = tmp_path / "tz.env"
    env.write_text("TZ=Etc/GMT-4\n", encoding="utf-8")
    try:
        config._load_dotenv(env)
        assert datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc).astimezone().utcoffset() == timedelta(hours=4)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        config._FROM_FILE.pop("TZ", None)
        time.tzset()
