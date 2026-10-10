"""mailer.py: the email itself, SMTP step by step over TLS, what each failure means, and reading the inbox read-only.

Every test runs against the fakes in email_fakes.py: a real connection fails the test.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
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
    ("connect", socket.gaierror(-2, "Name or service not known"), mailer.CONFIG),
    ("connect", ConnectionRefusedError(111, "Connection refused"), mailer.RETRY),
    ("connect", TimeoutError("timed out"), mailer.RETRY),
    ("connect", smtplib.SMTPConnectError(421, b"4.7.0 Try again later"), mailer.RETRY),
    ("mail", (553, b"5.7.1 Sender address rejected: not owned by user"), mailer.PROVIDER_BLOCK),
    ("mail", (550, b"Sender not allowed"), mailer.CONFIG),
    ("mail", (550, b"5.4.6 Daily sending limit exceeded"), mailer.PROVIDER_BLOCK),
    ("mail", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), mailer.RETRY),
    ("rcpt", (550, b"5.1.1 <lina@gulfbank.example>: User unknown"), mailer.BOUNCE),
    ("rcpt", (550, b"Mailbox not found"), mailer.BOUNCE),
    ("rcpt", (550, b"Account disabled"), mailer.BOUNCE),
    ("rcpt", (554, b"5.7.1 Relay access denied"), mailer.PROVIDER_BLOCK),
    ("rcpt", (451, b"4.7.1 Greylisted, try again"), mailer.RETRY),
    ("rcpt", (552, b"5.2.2 Mailbox full, quota exceeded"), mailer.FAILED),  # their mailbox, not ours: no pause
    ("rcpt", (550, b"5.4.5 Daily user sending quota exceeded"), mailer.PROVIDER_BLOCK),
    ("rcpt", socket.timeout("timed out"), mailer.RETRY),
    ("data", (554, b"5.7.1 Message rejected as spam"), mailer.PROVIDER_BLOCK),
    ("data", (552, b"5.3.4 Message size exceeds fixed limit"), mailer.FAILED),
    ("data", (452, b"4.3.1 Insufficient system storage"), mailer.RETRY),
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
        found, state = box.fetch_new("INBOX", None, lambda headers: True)
    assert found == [] and state == {"uidvalidity": 1, "last_uid": 1}
    assert "UID FETCH" not in offline_mail.imap_commands()
    reply = inbox.add(eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID))
    inbox.add(eml("unrelated.eml"))
    with mailer.Inbox(a_mailbox()) as box:
        found, state = box.fetch_new("INBOX", state, lambda h: OUR_ID in str(h.get("In-Reply-To")))
    assert [key for key, _ in found] == [f"uid:INBOX:1:{reply}"] and b"corporate rates" in found[0][1]
    assert state == {"uidvalidity": 1, "last_uid": 3}
    fetches = [e for e in offline_mail.log if e[:2] == ("imap", "UID FETCH")]
    assert [f[3] for f in fetches] == ["(UID BODY.PEEK[HEADER])", f"(UID BODY.PEEK[]<0.{mailer.INBOUND_BYTES_MAX}>)"]
    assert fetches[-1][2] == str(reply)  # only the relevant message is read in full
    with mailer.Inbox(a_mailbox()) as box:  # nothing new: no search at all
        assert box.fetch_new("INBOX", state, lambda h: True) == ([], state)
    assert set(offline_mail.imap_commands()) <= READ_ONLY


def test_a_renumbered_folder_is_read_again_since_the_last_check(offline_mail):
    inbox = offline_mail.folders["INBOX"]
    inbox.add(eml("reply_gmail.eml", lead=LEAD, ours=OUR_ID))
    inbox.uidvalidity = 7
    with mailer.Inbox(a_mailbox()) as box:
        found, state = box.fetch_new("INBOX", {"uidvalidity": 1, "last_uid": 40}, lambda h: True,
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
    assert mailer.opt_out_words("Please remove me from your list") == "remove me"
    assert mailer.opt_out_words("الرجاء إزالتي") and mailer.opt_out_words("لا تراسلني مرة اخرى")
    assert mailer.opt_out_words("stop") == "stop" and mailer.opt_out_words("توقف") == "توقف"
    long = "We stop by the airport most weeks and might need cars for visiting partners next month, thanks."
    assert mailer.opt_out_words(long) == ""  # "stop" counts only in a short reply
    assert mailer.opt_out_words("Sounds good", subject="Re: Stop waiting for taxis") == ""
    assert mailer.opt_out_words("", subject="Unsubscribe") == "unsubscribe"
    assert mailer.opt_out_words("Don’t email me again") == "don't email"  # a curly apostrophe


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
