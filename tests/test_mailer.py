"""mailer.py: the email itself, SMTP step by step over TLS, what each failure means, and reading the inbox read-only.

Every test runs against the fakes in email_fakes.py: a real connection fails the test.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
import socket
import ssl
from datetime import datetime, timezone
from email.header import decode_header, make_header

import pytest

from email_fakes import FROM, OUR_ID, a_mailbox, eml
from openberry import mailer

LEAD = "lina@gulfbank.example"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


def job(**changes) -> mailer.EmailJob:
    base = {"message_id": 7, "claim": "c1", "header_id": OUR_ID, "from_email": FROM, "from_name": "Sam Rahman",
            "to_email": LEAD, "to_name": "Lina Noor", "subject": "Airport transfers for your Dubai office",
            "body": "Hi Lina,\n\nSaw you're hiring in Dubai.\n\nSam", "footer": "Acme · 1 Road, Dubai\nReply "
            '"unsubscribe" and we won\'t email you again.'}
    return mailer.EmailJob(**{**base, **changes})


def parsed(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


# --------------------------------------------------------------------------------------
# The email exactly as it goes out
# --------------------------------------------------------------------------------------


def test_the_email_is_plain_text_exactly_as_approved_plus_the_footer():
    raw = mailer.build_email(job(), NOW).as_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")  # CRLF on the wire, no bare LF
    msg = parsed(raw)
    assert msg.get_content_type() == "text/plain" and not msg.is_multipart()
    assert msg.get_content_charset() == "utf-8" and msg["Content-Transfer-Encoding"] == "quoted-printable"
    assert msg.get_content().replace("\r\n", "\n") == (
        "Hi Lina,\n\nSaw you're hiring in Dubai.\n\nSam\n\nAcme · 1 Road, Dubai\n"
        'Reply "unsubscribe" and we won\'t email you again.\n')
    assert msg["Subject"] == "Airport transfers for your Dubai office"
    assert msg["Message-ID"] == OUR_ID and msg["Message-ID"].endswith("@acme-mail.example>")
    assert msg["Date"] and msg["From"] == "Sam Rahman <sam@acme-mail.example>" and msg["To"] == f"Lina Noor <{LEAD}>"
    token = "5f2b9c1e0a7d4e6b8c3f1a2d9e8b7c6a"  # the Message-ID's random part
    assert msg["List-Unsubscribe"] == f"<mailto:sam@acme-mail.example?subject=unsubscribe%20{token}>"
    for absent in ("X-Mailer", "Reply-To", "In-Reply-To", "References", "List-Unsubscribe-Post", "Precedence",
                   "Auto-Submitted"):
        assert msg[absent] is None, absent
    assert b"<html" not in raw.lower() and b"<img" not in raw.lower()


def test_names_are_encoded_and_cleaned_and_the_subject_is_never_changed():
    msg = parsed(mailer.build_email(job(from_name="سام رحمن", to_name="Evil\r\nBcc: x@y.example\x00 " + "N" * 200,
                                        subject="Transferts aéroport – Dubaï"), NOW).as_bytes())
    assert "=?utf-8?" in mailer.build_email(job(from_name="سام رحمن"), NOW).as_string()
    assert str(make_header(decode_header(msg["From"]))).startswith("سام رحمن")
    to = str(msg["To"])
    assert "\r" not in to and "\n" not in to and "Bcc" not in msg.keys()
    assert len(msg["To"].addresses[0].display_name) <= 78
    assert msg["Subject"] == "Transferts aéroport – Dubaï"
    with pytest.raises(ValueError, match="line break"):
        mailer.build_email(job(subject="Hello\r\nBcc: x@y.example"))
    with pytest.raises(ValueError, match="no subject"):
        mailer.build_email(job(subject="  "))


def test_a_company_lead_gets_the_address_alone_and_a_follow_up_joins_the_thread():
    msg = parsed(mailer.build_email(job(to_name="", references=("<a@acme-mail.example>", "<b@acme-mail.example>")),
                                    NOW).as_bytes())
    assert msg["To"] == LEAD
    assert msg["In-Reply-To"] == "<b@acme-mail.example>"
    assert msg["References"] == "<a@acme-mail.example> <b@acme-mail.example>"


def test_message_ids_are_random_on_the_senders_domain():
    first, second = mailer.new_header_id(FROM), mailer.new_header_id(FROM)
    assert first != second and first.endswith("@acme-mail.example>") and len(first.split("@")[0]) == 33


@pytest.mark.parametrize("address,ok", [
    ("lina@gulfbank.example", True), ("reservations+dxb@hotel.example", True), ("lina@gulfbank", False),
    ("lina gulfbank.example", False), ("ليـنا@bank.example", False), ("a" * 65 + "@x.example", False), ("", False),
])
def test_valid_addresses(address, ok):
    assert mailer.valid_address(address) is ok


@pytest.mark.parametrize("address", ["noreply@x.example", "no-reply@x.example", "do-not-reply@x.example",
                                     "mailer-daemon@x.example", "postmaster@x.example", "abuse@x.example",
                                     "bounces+123@x.example"])
def test_no_reply_and_system_addresses(address):
    assert mailer.no_reply_address(address)
    assert not mailer.no_reply_address("reservations@hotel.example")


# --------------------------------------------------------------------------------------
# SMTP: always encrypted, step by step
# --------------------------------------------------------------------------------------


def test_port_465_uses_tls_from_the_start_with_checked_certificates(offline_mail):
    outcome = mailer.send(job(), a_mailbox(), on_data=lambda: True, now=NOW)
    assert outcome.ok and outcome.data
    _, _, host, port, ehlo_name, timeout, kind, ctx = offline_mail.log[0]
    assert (host, port, kind) == ("smtp.acme-mail.example", 465, "FakeSMTPSSL")
    assert ehlo_name == "[127.0.0.1]" and timeout == mailer.TIMEOUT  # the computer's name stays out of the headers
    assert isinstance(ctx, ssl.SSLContext) and ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    assert offline_mail.smtp_commands() == ["connect", "ehlo", "login", "mail", "rcpt", "data", "quit"]
    assert offline_mail.sent == [{"to": LEAD, "data": outcome.data}]
    assert ("smtp", "mail", FROM) in offline_mail.log and ("smtp", "rcpt", LEAD) in offline_mail.log


def test_the_tls_context_checks_certificates_and_host_names():
    ctx = mailer.tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_port_587_starts_tls_before_the_password(offline_mail):
    assert mailer.send(job(), a_mailbox(smtp_port=587), on_data=lambda: True).ok
    assert offline_mail.log[0][3:7] == (587, "[127.0.0.1]", mailer.TIMEOUT, "FakeSMTP")
    commands = offline_mail.smtp_commands()
    assert commands[:5] == ["connect", "ehlo", "starttls", "ehlo", "login"]
    assert ("smtp", "login", True) in offline_mail.log  # logged in over TLS
    starttls = next(e for e in offline_mail.log if e[1] == "starttls")
    assert isinstance(starttls[2], ssl.SSLContext) and starttls[2].check_hostname


def test_a_server_without_starttls_never_gets_the_password(offline_mail):
    offline_mail.starttls = False
    outcome = mailer.send(job(), a_mailbox(smtp_port=587), on_data=lambda: True)
    assert outcome.kind == mailer.CONFIG and "Use port 465" in outcome.text
    assert "login" not in offline_mail.smtp_commands() and offline_mail.smtp_logins == []


def test_other_ports_are_refused_before_connecting(offline_mail):
    outcome = mailer.send(job(), a_mailbox(smtp_port=25), on_data=lambda: True)
    assert outcome.kind == mailer.CONFIG and offline_mail.log == []
    with pytest.raises(mailer.MailError) as caught:
        mailer.check_login(a_mailbox(imap_port=143))
    assert caught.value.kind == mailer.CONFIG and offline_mail.log == []


def test_on_data_false_means_rset_and_nothing_is_sent(offline_mail):
    outcome = mailer.send(job(), a_mailbox(), on_data=lambda: False)
    assert outcome.kind == mailer.ABORTED
    assert "data" not in offline_mail.smtp_commands() and "rset" in offline_mail.smtp_commands()
    assert offline_mail.sent == []


def test_a_non_ascii_password_signs_in_with_auth_plain_in_utf8(offline_mail):
    assert mailer.send(job(), a_mailbox(password="كلمة-سر"), on_data=lambda: True).ok
    assert ("smtp", "docmd", "AUTH") in offline_mail.log


def test_the_password_never_appears_in_outcomes_or_reprs(offline_mail):
    offline_mail.fail["login"] = smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication credentials invalid")
    box = a_mailbox(password="s3cret-Pass")
    outcome = mailer.send(job(), box, on_data=lambda: True)
    assert outcome.kind == mailer.AUTH and "s3cret" not in outcome.text and "s3cret" not in repr(box)


# --------------------------------------------------------------------------------------
# What a failure means (mailer.classify)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("step,failure,kind", [
    ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed"), mailer.AUTH),
    ("connect", ssl.SSLCertVerificationError("certificate verify failed: Hostname mismatch"), mailer.CONFIG),
    ("connect", ssl.SSLError("wrong version number"), mailer.CONFIG),
    ("connect", socket.gaierror(-2, "Name or service not known"), mailer.RETRY),  # a Wi-Fi drop as often as a typo
    ("connect", ConnectionRefusedError(111, "Connection refused"), mailer.RETRY),
    ("connect", TimeoutError("timed out"), mailer.RETRY),
    ("connect", smtplib.SMTPConnectError(421, b"4.7.0 Try again later"), mailer.RETRY),
    ("mail", (553, b"5.7.1 Sender address rejected: not owned by user"), mailer.CONFIG),  # R2-12: fix the From
    ("mail", (550, b"5.7.1 Relaying denied"), mailer.CONFIG),
    ("mail", (554, b"5.7.1 Your account is suspended for sending spam"), mailer.PROVIDER_BLOCK),
    ("mail", (550, b"Sender not allowed"), mailer.CONFIG),
    ("mail", (550, b"5.4.6 Daily sending limit exceeded"), mailer.PROVIDER_BLOCK),
    ("mail", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), mailer.RETRY),
    ("rcpt", (550, b"5.1.1 <lina@gulfbank.example>: User unknown"), mailer.BOUNCE),
    ("rcpt", (550, b"Mailbox not found"), mailer.BOUNCE),
    ("rcpt", (550, b"Account disabled"), mailer.BOUNCE),
    ("rcpt", (554, b"5.7.1 Relay access denied"), mailer.PROVIDER_BLOCK),
    # EMAIL-5: Postfix refuses the From address in reply to RCPT TO: fix the From, not a bounce nor a block.
    ("rcpt", (550, b"5.1.0 <sam@acme-mail.example>: Sender address rejected: User unknown in virtual mailbox table"),
     mailer.CONFIG),
    ("rcpt", (553, b"5.7.1 <sam@acme-mail.example>: Sender address rejected: not owned by user sam@acme-mail.example"),
     mailer.CONFIG),
    ("rcpt", (553, b"5.1.8 <sam@acme-mail.example>: Sender address rejected: Domain not found"), mailer.CONFIG),
    ("rcpt", (451, b"4.7.1 Greylisted, try again"), mailer.RETRY),
    ("rcpt", (552, b"5.2.2 Mailbox full, quota exceeded"), mailer.FAILED),  # their mailbox, not ours: no pause
    ("rcpt", (550, b"5.4.5 Daily user sending quota exceeded"), mailer.PROVIDER_BLOCK),
    ("rcpt", socket.timeout("timed out"), mailer.RETRY),
    # The server's answer after it received the whole email (smtplib returns it, it doesn't raise): R2UI-1.
    ("data", (554, b"5.7.1 Message rejected as spam"), mailer.PROVIDER_BLOCK),
    ("data", (550, b"5.7.1 Message refused by policy"), mailer.PROVIDER_BLOCK),
    ("data", (552, b"5.3.4 Message size exceeds fixed limit"), mailer.FAILED),
    ("data", (452, b"4.3.1 Insufficient system storage"), mailer.RETRY),
    ("data", (451, b"4.7.1 Try again later"), mailer.RETRY),
    ("data_start", (554, b"5.7.1 No DATA for you"), mailer.PROVIDER_BLOCK),
    ("data", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), mailer.UNKNOWN),
    ("data", TimeoutError("timed out"), mailer.UNKNOWN),
    ("data", ConnectionResetError(104, "Connection reset by peer"), mailer.UNKNOWN),
])
def test_each_failure_has_its_meaning(offline_mail, step, failure, kind):
    offline_mail.fail[step] = failure
    outcome = mailer.send(job(), a_mailbox(), on_data=lambda: True)
    assert outcome.kind == kind, outcome
    assert outcome.text and "\n" not in outcome.text
    if kind != mailer.UNKNOWN:
        assert offline_mail.sent == []


def test_server_words_are_kept_short_and_clean():
    text = mailer.server_text(b"550 bad\r\n\x1b[31m" + b"x" * 1000)
    assert "\x1b" not in text and "\n" not in text and len(text) <= mailer.SERVER_TEXT_MAX


def test_a_disconnect_before_data_is_retried_but_after_data_it_is_unknown():
    lost = smtplib.SMTPServerDisconnected("lost")
    assert mailer.classify(lost, "rcpt").kind == mailer.RETRY
    assert mailer.classify(lost, "data").kind == mailer.UNKNOWN
    assert "may have gone out" in mailer.classify(lost, "data").text.lower()


# --------------------------------------------------------------------------------------
# IMAP: read-only
# --------------------------------------------------------------------------------------

READ_ONLY = {"connect", "LOGIN", "AUTHENTICATE", "LIST", "EXAMINE", "STATUS", "UID SEARCH", "UID FETCH", "LOGOUT"}


def test_check_login_examines_the_inbox_read_only(offline_mail):
    mailer.check_login(a_mailbox())
    assert offline_mail.imap_commands() == ["connect", "LOGIN", "EXAMINE", "LOGOUT"]
    assert offline_mail.imap_logins == [(FROM, "app-password-1")]
    connect = next(e for e in offline_mail.log if e[0] == "imap")
    assert connect[3] == 993 and connect[4] == mailer.TIMEOUT


def test_separate_reading_details_are_used_for_imap(offline_mail):
    mailer.check_login(a_mailbox(imap_username="reader@acme-mail.example", imap_password="other"))
    assert offline_mail.imap_logins == [("reader@acme-mail.example", "other")]


def test_imap_login_failures_say_what_to_do(offline_mail):
    offline_mail.imap_fail["login"] = imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
    with pytest.raises(mailer.MailError) as caught:
        mailer.check_login(a_mailbox())
    assert caught.value.kind == mailer.AUTH and "IMAP access" in caught.value.text
    offline_mail.imap_fail["connect"] = ssl.SSLCertVerificationError("hostname mismatch")
    with pytest.raises(mailer.MailError) as caught:
        mailer.check_login(a_mailbox())
    assert caught.value.kind == mailer.CONFIG and "imap.acme-mail.example" in caught.value.text


def test_the_first_check_reads_no_history_then_only_new_relevant_mail(offline_mail):
    inbox = offline_mail.folders["INBOX"]
    inbox.add(eml("unrelated.eml"))  # history: never read
    with mailer.Inbox(a_mailbox()) as box:
        found, state, more = box.fetch_new("INBOX", None, lambda headers: True)
    assert found == [] and state == {"uidvalidity": 1, "last_uid": 1} and not more
    assert "UID FETCH" not in offline_mail.imap_commands()
    reply = inbox.add(eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID))
    inbox.add(eml("unrelated.eml"))
    with mailer.Inbox(a_mailbox()) as box:
        found, state, more = box.fetch_new("INBOX", state, lambda h: OUR_ID in str(h.get("In-Reply-To")))
    assert [key for key, _ in found] == [f"uid:INBOX:1:{reply}"] and b"corporate rates" in found[0][1]
    assert state == {"uidvalidity": 1, "last_uid": 3} and not more
    fetches = [e for e in offline_mail.log if e[:2] == ("imap", "UID FETCH")]
    assert [f[3] for f in fetches] == ["(UID BODY.PEEK[HEADER])", f"(UID BODY.PEEK[]<0.{mailer.INBOUND_BYTES_MAX}>)"]
    assert fetches[-1][2] == str(reply)  # only the relevant message is read in full
    with mailer.Inbox(a_mailbox()) as box:  # nothing new: no search at all
        assert box.fetch_new("INBOX", state, lambda h: True) == ([], state, False)
    assert set(offline_mail.imap_commands()) <= READ_ONLY


def test_a_backlog_bigger_than_one_batch_says_more_wait(offline_mail):
    """F3: after a weekend away, more than INBOX_FETCH_MAX new messages: fetch_new says so, and the next call goes on
    from where it stopped."""
    inbox = offline_mail.folders["INBOX"]
    with mailer.Inbox(a_mailbox()) as box:
        _, state, _ = box.fetch_new("INBOX", None, lambda h: True)
    for _ in range(5):
        inbox.add(eml("unrelated.eml"))
    with mailer.Inbox(a_mailbox()) as box:
        found, state, more = box.fetch_new("INBOX", state, lambda h: True, limit=3)
        assert len(found) == 3 and more and state["last_uid"] == 3
        found, state, more = box.fetch_new("INBOX", state, lambda h: True, limit=3)
        assert len(found) == 2 and not more and state["last_uid"] == 5


def test_a_folder_read_with_other_details_before_reads_since_the_last_check(offline_mail):
    """R2-2: a new IMAP server or username has no position yet: with first_since, the mail since the last check is
    read instead of nothing (a first check ever still reads nothing)."""
    inbox = offline_mail.folders["INBOX"]
    reply = inbox.add(eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID))
    with mailer.Inbox(a_mailbox()) as box:
        found, state, _ = box.fetch_new("INBOX", None, lambda h: True, first_since=datetime(2026, 10, 11).date())
    assert [key for key, _ in found] == [f"uid:INBOX:1:{reply}"] and state == {"uidvalidity": 1, "last_uid": 1}


def test_a_renumbered_folder_is_read_again_since_the_last_check(offline_mail):
    inbox = offline_mail.folders["INBOX"]
    inbox.add(eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID))
    inbox.uidvalidity = 7
    with mailer.Inbox(a_mailbox()) as box:
        found, state, _ = box.fetch_new("INBOX", {"uidvalidity": 1, "last_uid": 40}, lambda h: True,
                                        since=datetime(2026, 10, 11).date())
    assert len(found) == 1 and state["uidvalidity"] == 7
    assert ("imap", "UID SEARCH", "SINCE", "11-Oct-2026") in offline_mail.log


def test_folders_are_found_by_their_special_use_flags(offline_mail):
    offline_mail.folders = {"INBOX": offline_mail.folders["INBOX"], "INBOX.spam": offline_mail.folders["Junk"],
                            "Gesendet": offline_mail.folders["Sent"]}
    with mailer.Inbox(a_mailbox()) as box:
        assert box.folders() == {"inbox": "INBOX", "junk": "INBOX.spam", "sent": "Gesendet"}


def test_a_copy_of_a_sent_email_goes_to_sent_marked_as_read(offline_mail):
    assert mailer.save_sent_copy(a_mailbox(), b"raw email") == ""
    assert offline_mail.appended == [("Sent", b"raw email")]
    assert ("imap", "APPEND", "Sent", "(\\Seen)") in offline_mail.log
    offline_mail.imap_fail["append"] = imaplib.IMAP4.error(b"[OVERQUOTA] Quota exceeded")
    assert "Quota" in mailer.save_sent_copy(a_mailbox(), b"raw email")


def test_the_imap_dates_are_english_whatever_the_locale():
    assert mailer.imap_date(datetime(2026, 1, 5).date()) == "05-Jan-2026"


# --------------------------------------------------------------------------------------
# What came back
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,kind,text", [
    ("reply_gmail.eml", mailer.REPLY, "Thanks, this sounds useful. Can you send me your corporate rates?"),
    ("reply_outlook.eml", mailer.REPLY, "Hello Sam, please call me next week."),
    ("reply_apple.eml", mailer.REPLY, "What would 3 cars cost?"),
    ("reply_html_only.eml", mailer.REPLY, "Interesting & timely.\n\nSend me a quote."),
    ("reply_arabic.eml", mailer.REPLY, "نعم نحتاج سيارات للمؤتمر"),
    ("reply_subject_stop.eml", mailer.REPLY, "Sounds good, call me tomorrow."),
    ("stop.eml", mailer.OPT_OUT, "STOP"),
    ("optout_arabic.eml", mailer.OPT_OUT, "إلغاء الاشتراك"),
    ("unsubscribe_list.eml", mailer.OPT_OUT, ""),
    ("ooo_autosubmitted.eml", mailer.AUTO_REPLY, "out of the office"),
    ("ooo_subject.eml", mailer.AUTO_REPLY, "travelling"),
])
def test_replies_opt_outs_and_auto_replies(name, kind, text):
    inbound = mailer.classify_inbound(eml(name, lead=LEAD, ours=OUR_ID))
    assert inbound.kind == kind
    assert text in inbound.text
    assert inbound.from_email == LEAD
    if name != "unsubscribe_list.eml":
        assert OUR_ID in inbound.references
    for quoted in ("Saw you are hiring", "Hi Omar", "> Hi", "reply unsubscribe to stop", "Interested?"):
        assert quoted not in inbound.text  # the quoted part of our own email is gone


def test_the_list_unsubscribe_email_carries_our_token():
    inbound = mailer.classify_inbound(eml("unsubscribe_list.eml", lead=LEAD))
    assert inbound.kind == mailer.OPT_OUT and inbound.token == "5f2b9c1e0a7d4e6b8c3f1a2d9e8b7c6a"


@pytest.mark.parametrize("name,kind,status", [
    ("dsn_postfix.eml", mailer.BOUNCE, "5.1.1"),
    ("dsn_exchange.eml", mailer.BOUNCE, "5.1.10"),
    ("dsn_gmail.eml", mailer.BOUNCE, "5.1.1"),
    ("bounce_nonstandard.eml", mailer.BOUNCE, "5.1.1"),
    ("dsn_delayed.eml", mailer.SOFT_BOUNCE, "4.4.7"),
    ("dsn_policy.eml", mailer.POLICY_REJECT, "5.7.1"),
])
def test_bounces_are_told_apart(name, kind, status):
    inbound = mailer.classify_inbound(eml(name, lead=LEAD, ours=OUR_ID))
    assert (inbound.kind, inbound.status) == (kind, status)
    assert inbound.failed_recipients == (LEAD,)
    assert OUR_ID in inbound.original_ids and inbound.text == ""


def test_unrelated_and_malformed_mail():
    assert mailer.classify_inbound(eml("unrelated.eml")).from_email == "news@shop.example"
    malformed = mailer.classify_inbound(eml("malformed.eml"))  # unreadable: no sender, nothing to match
    assert malformed.from_email == "" and malformed.references == ()


def test_opt_out_words_in_english_and_arabic():
    assert mailer.read_opt_out("Please remove me from your list") == (mailer.SURE, "Please remove me from your list")
    assert mailer.read_opt_out("الرجاء إزالتي")[0] == mailer.SURE
    assert mailer.read_opt_out("لا تراسلني مرة اخرى")[0] == mailer.SURE
    assert mailer.opt_out_words("stop") == "stop" and mailer.opt_out_words("توقف") == "توقف"
    long = "We stop by the airport most weeks and might need cars for visiting partners next month, thanks."
    assert mailer.read_opt_out(long) == ("", "")  # "stop" counts only on its own
    assert mailer.read_opt_out("Sounds good", subject="Re: Stop waiting for taxis") == ("", "")
    assert mailer.read_opt_out("", subject="Unsubscribe") == (mailer.SURE, "Unsubscribe")
    assert mailer.read_opt_out("Don’t email me again") == (mailer.SURE, "Don’t email me again")  # a curly apostrophe
    # A subject typed after ours, and a new email whose subject is all it says.
    assert mailer.read_opt_out("", subject="RE: Airport transfers for your guests - UNSUBSCRIBE")[0] == mailer.SURE
    assert mailer.read_opt_out("", subject="REMOVE")[0] == mailer.SURE
    # A subject of their own that says something else: not sure, even if the text is one word.
    assert mailer.read_opt_out("Stop", subject="Booking for Friday")[0] == mailer.MAYBE


def test_opt_out_words_are_shown_as_they_were_written():
    """R2UI-9: the never-email list and the lead page show what the person wrote, not the folded text."""
    level, words = mailer.read_opt_out("شكرا. لا ترسل لي رسائل بعد الآن")
    assert level == mailer.SURE and words == "لا ترسل لي رسائل بعد الآن"
    assert mailer.read_opt_out("Sure, but NO MORE EMAILS after Friday's booking please")[1] == "NO MORE EMAILS"


# The reviewers' corpus of phrases (round 2: R2-4, INBOX2-1, INBOX2-3, R2UI-2 and their batteries). A reply that says
# nothing but an opt-out acts by itself; one with more around it is flagged for the user; a booking reply never acts.
SURE_OPT_OUTS = [
    "STOP", "Stop.", "Please stop", "No thanks, stop.", "Not interested, please remove me from your mailing list.",
    "Stop emailing", "Please stop sending me these emails", "Unsubscribe", "Take me off your list",
    "Do not contact me again", "Opt me out please", "No more emails", "الرجاء إلغاء الاشتراك", "توقف",
    "أزلني من قائمتكم", "كفى", "Please remove me from your emails.", "Please remove me, not interested.",
    "Remove me from your distribution list.", "Please remove me from future emails", "Please stop sending.",
    "Stop sending them please.", "Please take me off.", "Take me off your emails.", "I'd like to opt out.",
    "We'd like to opt out of these emails.", "Do not contact again.", "Please do not contact this address again.",
    "الرجاء إزالتي من قائمتكم", "لا ترسلوا رسائل مرة أخرى", "Please remove us from your distribution list.",
    "We are not interested. Please do not send any more emails.", "I'd like to opt out of these emails.",
    "Kindly remove this email address from your records.", "Not interested, don't contact again.",
    "Stop spamming us.", "Stop spamming us. We never asked for this.",
    "We do not wish to receive further communication", "Do not write to me again",
    "Who gave you my email? Do not write to me again.", "يرجى إزالة بريدي الإلكتروني من قائمتكم البريدية",
    "لا نرغب في استلام أي رسائل منكم", "لا نرغب في استلام أي رسائل منكم مستقبلاً.", "الرجاء عدم مراسلتنا مرة أخرى",
    "لا ترسلون لنا ايميلات", "لو سمحت لا ترسلون لنا ايميلات مرة ثانية.", "يرجى عدم التواصل معنا", "Remove",
    "REMOVE ME FROM THIS", "Remove from list", "Please remove.", "Unsub", "Do not contact.", "Don't email again",
    "Please do not email this address", "Please stop emailing this address",
    "Kindly stop sending emails to this address", "We are not interested, kindly don't send us emails anymore.",
    "Please cease all communication.", "Please refrain from contacting us.", "Take this address off your list",
    "No more emails from you please", "I never subscribed. Delete my email.",
    "Please remove info@hotel.example from your mailing list", "Stop it", "لا ترسلوا لنا شيئاً بعد الآن",
    "الرجاء حذف بريدي من قائمتكم", "ألغوا اشتراكي", "لا تراسلونا", "أرجو عدم إرسال رسائل أخرى",
    "غير مهتمين، يرجى عدم الإرسال مرة أخرى", "الرجاء إلغاء الاشتراك وعدم مراسلتي مرة أخرى.",
    "Hi Sam,\n\nPlease unsubscribe me.\n\nRegards,\nNoura\nExecutive Assistant to the GM\n+971 4 555 0101",
    "Unsubscribe\n\nSent from my iPhone", "لا تتواصل معي",
]
FLAGGED_OPT_OUTS = [  # real opt-outs with more around them: never missed, the user decides
    "How do I unsubscribe?", "Thanks.\nRemove me\nSam", "لا تتواصل معي، تواصل مع مديرة المكتب سارة",
    "Please delete my details from your database and confirm you have done so.",
    "Mr. Omar asked me to tell you he is not interested. Please remove him from your mailing list and do not contact "
    "him again.",
    "Stop. We already booked with another company.",
    "Mr Karim is travelling this month. Please stop sending him offers; I handle guest transport.",  # his assistant
    "Please stop sending us offers, we have a contract with another company.",
]
BOOKING_REPLIES = [  # never an automatic opt-out
    "How much for DXB to Marina with one stop?", "Can your driver stop at the mall first?",
    "Yes please. Need a non-stop transfer to Abu Dhabi tomorrow.", "كم السعر مع توقف في المول؟",
    "Please don't unsubscribe me, I want your rates next month.",
    "Can we opt out of the meet and greet to lower the price? Please send a quote.",
    "please do not send me the brochure, just the price", "Do not contact the GM directly, email me instead.",
    "Please remove me from tomorrow's pickup list, guest cancelled.", "لا ترسل السائق قبل الساعة 6",
    "لا تتصل بي، أرسل لي عرض السعر بالبريد", "السائق توقف عن الرد، من فضلك اتصل بي",
    "Please don't contact me on my mobile, I'm in meetings all day. Email is best: send the rate card here.",
    "Please stop contacting our front desk directly. Send all quotes to me. We need 3 S-Class for 21 Oct.",
    "Also, please remove my address from the pickup, the guest will come to the hotel. Flight EK 512.",
    "Please don't email me the invoices, send them to accounts@. And confirm the 3 cars for Friday.",
    "لا تتواصل معي على الواتساب، راسلني على الإيميل فقط", "توقف عن إرسال السائق إلى العنوان القديم",
    "please stop sending this driver to our guests, he was late twice. Book 2 V-Class for 19 Oct.",
    "We'd like to unsubscribe from the monthly airport package and switch to pay-per-trip.",
    "I don't want to unsubscribe, but once a month is enough.",
    "Please don't message me on LinkedIn, email is fine. Send the rates.",
    "Do not contact me before 9am, I'm on night shift; email the quote.",
    "Stop messaging the driver's number, he can't answer while driving. Message me instead.",
    "No more emails needed, I'll call you Sunday to book the 3 cars.",
    "نريد إلغاء الاشتراك الشهري للسيارات والتحويل إلى الدفع لكل رحلة",
    "لا تتواصل معي، تواصل مع مديرة المكتب سارة لحجز السيارات",
    "Please take us off your list of pending quotes for October and add us for November: 4 cars.",
    "Please remove us from the waiting list for the 18th, we found a car. Keep us for the 25th.",
    "نعم، نحتاج سيارتين يوم الجمعة من المطار إلى الفندق. لا ترسل لي رسائل نصية، البريد الإلكتروني أفضل",
    "Yes, we need a regular airport transfer partner. Please don't email me here, send your rate card and the "
    "booking confirmation to fom@oldcreek-hotel.example",
    "No more emails needed, I will call you to confirm the booking.", "Kindly remove my address from the guest list "
    "for Friday", "Please do not message me on LinkedIn, email is better.",
    "Stop sending these quotes to my personal email, send them to bookings@hotel.example",
    "Do not contact me on this number, WhatsApp only.", "remove me from tomorrow's pickup list",
    "opt out of the meet and greet service", "don't send the driver before 6", "take me off the pickup list for tomorrow",
    "stop sending cars to the old address", "Stop at the Marriott first", "لا ترسلوا السائق قبل السادسة",
    "Please don't send the car before 9",
]
NOT_EVEN_FLAGGED = [  # booking replies that don't even look like an opt-out
    "How much for DXB to Marina with one stop?", "Can your driver stop at the mall first?", "Stop at the Marriott first",
    "Please don't send the car before 9", "don't send the driver before 6", "stop sending cars to the old address",
    "Please stop sending him to the old terminal, his flights now land at Terminal 3.",
    "Please remove me from tomorrow's pickup list, guest cancelled.", "take me off the pickup list for tomorrow",
    "Please take us off your list of pending quotes for October and add us for November: 4 cars.",
    "Please remove us from the waiting list for the 18th, we found a car. Keep us for the 25th.",
    "Kindly remove my address from the guest list for Friday", "Do not contact me on this number, WhatsApp only.",
    "Please don't message me on LinkedIn, email is fine. Send the rates.",
    "No more emails needed, I'll call you Sunday to book the 3 cars.",
    "نعم، نحتاج سيارتين يوم الجمعة من المطار إلى الفندق. لا ترسل لي رسائل نصية، البريد الإلكتروني أفضل",
    "Please don't contact me on my mobile, I'm in meetings all day. Email is best: send the rate card here.",
    "Please stop contacting our front desk directly. Send all quotes to me. We need 3 S-Class for 21 Oct.",
    "I don't want to unsubscribe, but once a month is enough.",
    "We'd like to unsubscribe from the monthly airport package and switch to pay-per-trip.",
    "نريد إلغاء الاشتراك الشهري للسيارات والتحويل إلى الدفع لكل رحلة",
    "لا تتواصل معي على الواتساب، راسلني على الإيميل فقط", "توقف عن إرسال السائق إلى العنوان القديم",
    "Please don't unsubscribe me, I want your rates next month.", "لا ترسل السائق قبل الساعة 6",
    "Do not contact the GM directly, email me instead.", "please do not send me the brochure, just the price",
]


@pytest.mark.parametrize("text", SURE_OPT_OUTS, ids=lambda text: text[:24])  # short ids: Arabic is escaped
def test_a_reply_that_says_only_that_is_an_opt_out(text):
    assert mailer.read_opt_out(text)[0] == mailer.SURE, text


@pytest.mark.parametrize("text", FLAGGED_OPT_OUTS, ids=lambda text: text[:24])  # short ids: Arabic is escaped
def test_an_opt_out_with_more_around_it_is_flagged_for_the_user(text):
    assert mailer.read_opt_out(text)[0] == mailer.MAYBE, text


@pytest.mark.parametrize("text", BOOKING_REPLIES, ids=lambda text: text[:24])  # short ids: Arabic is escaped
def test_a_booking_reply_is_never_an_automatic_opt_out(text):
    assert mailer.read_opt_out(text)[0] != mailer.SURE, text


@pytest.mark.parametrize("text", NOT_EVEN_FLAGGED, ids=lambda text: text[:24])  # short ids: Arabic is escaped
def test_a_booking_reply_doesnt_even_look_like_an_opt_out(text):
    assert mailer.read_opt_out(text) == ("", ""), text


def test_our_own_footer_left_in_a_reply_is_no_opt_out():
    footer = 'Acme Chauffeurs · Office 1204, Dubai\nNot interested? Reply "unsubscribe" and we won\'t email you again.'
    pasted = ('Yes, send the rates.\n\nHi Lina, we drive executives.\nAcme Chauffeurs · Office 1204, Dubai\n'
              'Not interested?  Reply "unsubscribe" and we won\'t email you again.')
    assert mailer.read_opt_out(pasted, ignore=[footer]) == ("", "")
    # The footer's instructions quoted alone ('Reply "unsubscribe"') aren't the person asking either.
    assert mailer.read_opt_out('Yes, send the rates.\nNot interested? Reply "unsubscribe" to opt out.') == ("", "")


def test_a_footer_line_never_swallows_a_one_word_opt_out():
    """R2-9: a footer may end in a line that is just "Unsubscribe"; a reply that says just that is still an opt-out.
    Footers are removed only as a whole block."""
    footer = "Acme Chauffeurs, Bay Square, Dubai\nUnsubscribe"
    assert mailer.read_opt_out("Unsubscribe", ignore=[footer]) == (mailer.SURE, "Unsubscribe")
    assert mailer.read_opt_out("Unsubscribe", ignore=["Unsubscribe"]) == (mailer.SURE, "Unsubscribe")
    assert mailer.read_opt_out(f"Thanks, see you Friday.\n\n{footer}", ignore=[footer]) == ("", "")
    raw = (f"From: Lina <{LEAD}>\r\nTo: {FROM}\r\nSubject: Re: Cars\r\nMessage-ID: <r9@x>\r\nIn-Reply-To: {OUR_ID}\r\n"
           "Content-Type: text/plain; charset=utf-8\r\n\r\nUnsubscribe\r\n").encode()
    assert mailer.classify_inbound(raw, [footer]).kind == mailer.OPT_OUT


def test_quoted_text_and_html_are_reduced_to_what_is_new():
    assert mailer.strip_quoted("Yes!\n\n-----Original Message-----\nFrom: Sam\nHi") == "Yes!"
    assert mailer.strip_quoted("Thanks\n> quoted\nSee you") == "Thanks\nSee you"
    assert mailer.strip_quoted("Ok\nFrom: Sam Rahman <sam@x.example>\nSent: Monday\nTo: Lina\nOld") == "Ok"
    assert mailer.strip_quoted("x" * 70_000).startswith("x") and len(mailer.strip_quoted("x" * 70_000)) == 50_000
    html = '<p>Hi<br>there</p><script>alert(1)</script><img src="https://t.example/p.gif"><blockquote>old</blockquote>'
    assert mailer.html_to_text(html) == "Hi\nthere"


def test_header_facts_say_what_to_read_in_full():
    headers = email.message_from_bytes(eml("dsn_postfix.eml", lead=LEAD, ours=OUR_ID).split(b"\r\n\r\n")[0],
                                       policy=email.policy.default)
    facts = mailer.header_facts(headers)
    assert facts["bounce"] and facts["from_email"].startswith("mailer-daemon@")
    reply = email.message_from_bytes(eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID), policy=email.policy.default)
    assert OUR_ID in mailer.header_facts(reply)["references"] and not mailer.header_facts(reply)["bounce"]



# --------------------------------------------------------------------------------------
# What real mail looks like
# --------------------------------------------------------------------------------------


def outlook_html_reply(new_text: str) -> bytes:
    """An HTML-only reply from Outlook on the web: the From/Sent block in divRplyFwdMsg, our email after it."""
    html = (f'<html><body><div class="elementToProof">{new_text}</div><div id="appendonsend"></div><hr>'
            '<div id="divRplyFwdMsg" dir="ltr"><b>From:</b> Sam Rahman &lt;sam@acme-mail.example&gt;<br><b>Sent:'
            '</b> Monday<br><b>Subject:</b> Airport transfers</div><div class="BodyFragment"><div>Hi Lina,<br>We '
            'drive executives.<br>Not interested? Reply "unsubscribe" and we won\'t email you again.</div></div>'
            '</body></html>')
    return (f"From: Lina Noor <{LEAD}>\r\nTo: {FROM}\r\nSubject: RE: Airport transfers\r\nMessage-ID: <owa-1@x>\r\n"
            f"In-Reply-To: {OUR_ID}\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html}"
            ).encode()


def test_an_outlook_html_reply_keeps_only_the_new_text_and_our_footer_is_no_opt_out():
    inbound = mailer.classify_inbound(outlook_html_reply("Yes, please send me your corporate rates."))
    assert (inbound.kind, inbound.text) == (mailer.REPLY, "Yes, please send me your corporate rates.")
    html = eml("reply_outlook.eml", lead=LEAD, ours=OUR_ID).decode().split("text/html")[1]
    assert mailer.html_to_text(html.split("\r\n\r\n", 1)[1]) == "Hello Sam, please call me next week."


def test_trip_details_in_a_reply_are_kept():
    english = ("Yes, please quote this trip:\nFrom: Dubai Marina, Tower 3\nTo: DXB Terminal 3\nDate: 15 Oct, 06:00\n"
               "Passengers: 3, 4 bags\n\nThanks, Lina")
    arabic = "نعم، أرجو عرض سعر:\nمن: دبي مارينا\nإلى: مطار دبي مبنى 3\nالتاريخ: 15 أكتوبر"
    office = "مرحبا سام،\nعنوان المكتب:\nبرج الخليج، الطابق 12"
    for text in (english, arabic, office):
        assert mailer.strip_quoted(text) == text
    # Quoted headers are still cut: an address in From:, or a Subject: below it.
    assert mailer.strip_quoted("Ok\nFrom: Sam Rahman\nSent: Monday\nTo: Lina\nSubject: Cars\nOld") == "Ok"
    assert mailer.strip_quoted("نعم\nفي الاثنين، كتب سام <sam@acme-mail.example>:\n> قديم") == "نعم"


@pytest.mark.parametrize("status,diagnostic,kind", [
    ("5.4.1", "smtp; 550 5.4.1 Recipient address rejected: Access denied. AS(201806281)", mailer.BOUNCE),
    ("5.4.4", "smtp; 550 5.4.4 Host or domain name not found", mailer.BOUNCE),
    ("5.7.1", "smtp; 550 5.7.1 RESOLVER.RST.AuthRequired; authentication required", mailer.BOUNCE),
    ("5.7.133", "smtp; 550 5.7.133 RESOLVER.RST.SenderNotAuthenticatedForGroup", mailer.BOUNCE),
    ("5.7.1", "smtp; 554 5.7.1 Message rejected as spam", mailer.POLICY_REJECT),
    ("4.4.7", "smtp; 450 4.4.7 Delivery delayed", mailer.SOFT_BOUNCE),
])
def test_microsoft_365_and_postfix_permanent_failures(status, diagnostic, kind):
    raw = eml("dsn_postfix.eml", lead=LEAD, ours=OUR_ID).decode()
    raw = re.sub(r"Status: 5\.1\.1", f"Status: {status}", raw)
    raw = re.sub(r"Diagnostic-Code: .*(\r\n[ \t].*)*", f"Diagnostic-Code: {diagnostic}", raw)
    assert mailer.classify_inbound(raw.encode()).kind == kind, (status, diagnostic)


def test_a_failure_at_rcpt_for_a_microsoft_365_address_that_doesnt_exist_is_a_bounce():
    refused = smtplib.SMTPRecipientsRefused({LEAD: (550, b"5.4.1 Recipient address rejected: Access denied")})
    assert mailer.classify(refused, "rcpt").kind == mailer.BOUNCE


def test_a_relayed_dsn_is_no_bounce():
    raw = eml("dsn_postfix.eml", lead=LEAD, ours=OUR_ID).decode()
    raw = re.sub(r"Action: failed", "Action: relayed", raw)
    raw = re.sub(r"Status: 5\.1\.1", "Status: 2.0.0", raw)
    assert mailer.classify_inbound(raw.encode()).kind == mailer.AUTO_REPLY


def test_a_newsletter_from_a_leads_address_is_no_reply():
    raw = (f"From: Palm Hotel <info@palmhotel.example>\r\nTo: {FROM}\r\nSubject: Our autumn offers\r\n"
           "Message-ID: <campaign-1@mail.example>\r\nList-Id: <palm.list.example>\r\n"
           "List-Unsubscribe: <https://example.com/u/1>\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           '<p>New suites!</p><a href="https://example.com/u/1">Unsubscribe</a>').encode()
    assert mailer.classify_inbound(raw).kind == mailer.AUTO_REPLY
    # A reply through a mailing list (a Google Group) still refers to our email: it is read (its "Unsubscribe" link
    # text next to other words is only flagged).
    reply = raw.replace(b"Subject: Our autumn offers", f"Subject: Re: Cars\r\nIn-Reply-To: {OUR_ID}".encode())
    inbound = mailer.classify_inbound(reply)
    assert (inbound.kind, inbound.opt_out_hint) == (mailer.REPLY, "Unsubscribe")


def test_a_reply_from_a_collaborative_inbox_is_a_reply():
    """INBOX2-5: Google Workspace's collaborative inbox adds "Precedence: list" and List-Id to a person's reply."""
    raw = (f"From: Hilton Reservations <reservations@hotel.example>\r\nTo: {FROM}\r\nSubject: Re: Airport transfers\r\n"
           f"Message-ID: <grp-1@hotel.example>\r\nIn-Reply-To: {OUR_ID}\r\nPrecedence: list\r\n"
           "List-Id: <reservations.hotel.example>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           "Hi Sam, we'd like a quote for 5 airport transfers next week.\r\n").encode()
    inbound = mailer.classify_inbound(raw)
    assert inbound.kind == mailer.REPLY and "5 airport transfers" in inbound.text
    newsletter = raw.replace(f"In-Reply-To: {OUR_ID}\r\n".encode(), b"")  # refers to none of ours: a list's mail
    assert mailer.classify_inbound(newsletter).kind == mailer.AUTO_REPLY


def test_a_fetch_answer_with_the_uid_after_the_message_is_read(offline_mail, monkeypatch):
    fake = imaplib.IMAP4_SSL
    original = fake.uid

    def uid_last(self, command, *args):  # "* 1 FETCH (BODY[] {n}<message> UID 5)": RFC 3501 allows any order
        typ, data = original(self, command, *args)
        if command != "FETCH":
            return typ, data
        out = []
        for item in data:
            if isinstance(item, tuple):
                uid = re.search(rb"UID (\d+)", item[0]).group(1)
                out.append((re.sub(rb"UID \d+ ", b"", item[0]), item[1]))
                out.append(b" UID " + uid + b")")
        return typ, out

    monkeypatch.setattr(fake, "uid", uid_last)
    inbox = offline_mail.folders["INBOX"]
    with mailer.Inbox(a_mailbox()) as box:
        _, state, _ = box.fetch_new("INBOX", None, lambda h: True)
    uid = inbox.add(eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID))
    with mailer.Inbox(a_mailbox()) as box:
        found, _, _ = box.fetch_new("INBOX", state, lambda h: True)
    assert [key for key, _ in found] == [f"uid:INBOX:1:{uid}"] and b"corporate rates" in found[0][1]


def test_a_message_without_a_message_id_has_the_same_key_wherever_it_is_read():
    raw = re.sub(rb"Message-ID: [^\r\n]*\r\n", b"", eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID), flags=re.I)
    assert mailer.classify_inbound(raw).message_id == ""
    assert mailer.content_key(raw) == mailer.content_key(b"X-Spam-Flag: NO\r\n" + raw)  # moved from Junk
    assert mailer.content_key(raw) != mailer.content_key(raw.replace(b"corporate rates", b"group rates"))


def test_list_unsubscribe_is_written_as_an_address_on_the_wire(offline_mail):
    sender = "bookings@connexionlimodubai.com"
    outcome = mailer.send(job(from_email=sender, header_id="<5f2b9c1e0a7d4e6b8c3f1a2d9e8b7c6a@connexionlimodubai.com>"),
                          a_mailbox(from_email=sender, username=sender), on_data=lambda: True)
    assert outcome.ok
    assert f"List-Unsubscribe: <mailto:{sender}?subject=unsubscribe%20".encode() in outcome.data
    assert b"=?utf-8?" not in outcome.data
    assert all(len(line) <= 78 for line in outcome.data.split(b"\r\n\r\n", 1)[1].split(b"\r\n"))  # the body
    assert parsed(outcome.data).get_content().startswith("Hi Lina,")


def test_a_certificate_for_another_name_says_so_in_plain_words():
    outcome = mailer.classify(ssl.SSLCertVerificationError(1, "certificate verify failed: Hostname mismatch"),
                              "connect", "mail.desertline.example")
    assert outcome.kind == mailer.CONFIG and "CERTIFICATE_VERIFY_FAILED" not in outcome.text
    assert "security certificate isn't valid for mail.desertline.example" in outcome.text
    assert mailer.classify(ValueError("port 25 isn't used: only 465 or 587"), "connect").kind == mailer.CONFIG


@pytest.mark.parametrize("sep", ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "])
def test_a_subject_with_any_line_break_is_refused(sep):
    assert mailer.subject_breaks(f"Airport transfers{sep}for Gulf Bank")
    with pytest.raises(ValueError, match="line break"):
        mailer.build_email(job(subject=f"Airport transfers{sep}for Gulf Bank"))
    assert not mailer.subject_breaks("Airport transfers – Dubaï office")
