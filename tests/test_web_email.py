"""Email sending in the dashboard, the MCP server and the JSON API: the Mailbox page (write-only passwords, presets,
test email, inbox check), the Outreach page's Email sending card, the pills and lines under each email, the lead
page, and what Claude and scripts may (not) change. Mail servers are fakes (email_fakes.py): no network."""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from email_fakes import FROM, IMAP_HOST, SMTP_HOST, connect_mailbox
from openberry import config, db, mailer, mcp_server, repo, services
from openberry.mcp_server import build_server
from openberry.models import CompanyIn, LeadIn
from openberry.web.app import create_app

CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)">')
FOOTER = ('Acme Chauffeurs · Office 1204, Bay Square, Dubai\n'
          'Not interested? Reply "unsubscribe" and we won\'t email you.')
PASSWORD = "Zoho-App-Pass#1 'x'"
ALL_HOURS = {"email_days": [0, 1, 2, 3, 4, 5, 6], "email_start_hour": 0, "email_end_hour": 24}


@pytest.fixture
def web_settings(settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offline_mail):
    settings.scheduler_enabled = False
    settings.http_mcp_enabled = False
    settings.allowed_hosts = ["testserver"]
    monkeypatch.setenv("OPENBERRY_ENV_FILE", str(tmp_path / "openberry.env"))
    monkeypatch.setattr(config, "_FROM_FILE", {})
    monkeypatch.setattr(config, "_settings_file_seen", None)
    monkeypatch.setattr(services, "_sending_stopped", "")
    for name in config.DASHBOARD_SETTINGS:  # unset now, and removed again after the test
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return settings


@pytest.fixture
def client(web_settings):
    with TestClient(create_app(web_settings)) as c:
        yield c


def token(client: TestClient, path: str = "/mailbox") -> str:
    match = CSRF_META.search(client.get(path).text)
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def post(client: TestClient, path: str, data: dict[str, Any], page: str = "/mailbox"):
    return client.post(path, data={**data, "csrf_token": token(client, page)}, follow_redirects=False)


def flashes(page: str) -> str:
    return html_lib.unescape(" ".join(re.findall(r'class="flash[^"]*"[^>]*>.*?<p>(.*?)</p>', page, re.S)))


def follow(client: TestClient, resp) -> str:
    assert resp.status_code == 303, resp.text[:500]
    return client.get(resp.headers["location"]).text


def env_file() -> Path:
    return Path(os.environ["OPENBERRY_ENV_FILE"])


MAILBOX_FORM = {"action": "save", "provider": "zoho", "region": "com", "from_email": FROM, "from_name": "Sam Rahman",
                "username": "", "password": PASSWORD, "smtp_host": "", "smtp_port": "465", "imap_host": "",
                "save_sent": "true"}


def save_mailbox(client: TestClient, **changes: str):
    return post(client, "/mailbox", {**MAILBOX_FORM, **changes})


def card(page: str) -> str:
    match = re.search(r'<section class="card agent-card email-card" id="email".*?</section>', page, re.S)
    assert match, "no Email sending card"
    return match.group(0)


def ready(company, **outreach: Any):
    """A connected mailbox with a fresh inbox check, and email sending on for the company."""
    connect_mailbox(config.get_settings())
    repo.save_inbox_check(FROM, True)
    return repo.update_company(company.id, {"outreach": {"email_sending": True, "email_footer": FOOTER, **ALL_HOURS,
                                                         **outreach}})


def lead_with_email(company, email_address: str = "lina@gulfbank.example", name: str = "Lina Noor") -> int:
    return repo.upsert_lead(company.id, LeadIn(full_name=name, email=email_address, lead_company="Gulf Bank"))[0].id


# --------------------------------------------------------------------------------------
# The Mailbox page
# --------------------------------------------------------------------------------------


def test_the_mailbox_page_offers_the_presets_with_their_warnings(client):
    page = client.get("/mailbox").text
    assert "Zoho Mail, paid plan (your own domain)" in page and "Hostinger Email" in page and "Gmail" in page
    assert "Microsoft" not in page  # OAuth mailboxes aren't supported
    presets = json.loads(html_lib.unescape(re.search(r"data-presets='([^']+)'", page).group(1)))
    assert presets["zoho"] == {"smtp": "smtppro.zoho.{region}", "port": 465, "imap": "imappro.zoho.{region}",
                               "save_sent": True}
    assert presets["zoho_personal"]["smtp"] == "smtp.zoho.{region}" and presets["zoho_personal"]["port"] == 587
    assert presets["hostinger"] == {"smtp": "smtp.hostinger.com", "port": 465, "imap": "imap.hostinger.com",
                                    "save_sent": True}
    assert presets["gmail"]["save_sent"] is False
    text = html_lib.unescape(page)
    assert "Hostinger doesn't allow emails to people who didn't ask for them" in text
    assert "doesn't allow bulk or burst sending" in text and "50 and 500 an hour" in text
    assert "zoho.eu (Europe)" in text and "zoho.com.au (Australia)" in text
    assert 'name="password" type="password" value=""' in page


def test_saving_the_mailbox_writes_the_data_folders_env_file_and_checks_the_inbox(client, offline_mail):
    page = follow(client, save_mailbox(client))
    assert "Mailbox saved. Inbox checked: OpenBerry can read it." in flashes(page)
    settings = config.get_settings()
    assert (settings.smtp_host, settings.smtp_port, settings.imap_host) == ("smtppro.zoho.com", 465, "imappro.zoho.com")
    assert settings.smtp_username == FROM and settings.smtp_password == PASSWORD
    text = env_file().read_text()
    assert 'OPENBERRY_SMTP_HOST="smtppro.zoho.com"' in text and "OPENBERRY_IMAP_SAVE_SENT=\"true\"" in text
    if sys.platform != "win32":
        assert stat.S_IMODE(env_file().stat().st_mode) == 0o600
    assert config.read_dotenv(env_file())["OPENBERRY_SMTP_PASSWORD"] == PASSWORD  # read back exactly
    assert offline_mail.imap_logins == [(FROM, PASSWORD)] and "STATUS" in offline_mail.imap_commands()
    assert repo.mailbox_state(FROM)["inbox_ok"]
    # The password is never shown again, anywhere.
    for path in ("/mailbox", "/help", "/companies"):
        assert PASSWORD not in client.get(path).text and html_lib.escape(PASSWORD) not in client.get(path).text
    assert "Saved. Type a new one to change it." in client.get("/mailbox").text
    with db.connect() as c:
        for table in ("companies", "messages", "email_mailbox", "email_events"):
            assert PASSWORD not in json.dumps([list(r) for r in c.execute(f"SELECT * FROM {table}")], default=str)


def test_an_empty_password_keeps_the_saved_one_and_regions_and_presets_fill_the_servers(client):
    save_mailbox(client)
    follow(client, save_mailbox(client, password="", smtp_port="587", from_name="Sam R."))  # same servers
    settings = config.get_settings()
    assert settings.smtp_password == PASSWORD and settings.smtp_port == 587 and settings.smtp_from_name == "Sam R."
    follow(client, save_mailbox(client, password=PASSWORD, provider="zoho_personal", region="eu", smtp_port="587"))
    assert (settings.smtp_host, settings.smtp_port, settings.imap_host) == ("smtp.zoho.eu", 587, "imap.zoho.eu")
    follow(client, save_mailbox(client, provider="hostinger"))
    assert (settings.smtp_host, settings.imap_host) == ("smtp.hostinger.com", "imap.hostinger.com")
    follow(client, save_mailbox(client, provider="other", smtp_host="Mail.Example.COM", imap_host="imap.example.com"))
    assert settings.smtp_host == "mail.example.com" and settings.mail_provider == "other"


@pytest.mark.parametrize("changes", [
    {"provider": "other", "smtp_host": SMTP_HOST, "imap_host": "mail.attacker.example"},
    {"provider": "other", "smtp_host": "mail.attacker.example", "imap_host": IMAP_HOST},
    {"provider": "other", "smtp_host": SMTP_HOST, "imap_host": IMAP_HOST, "username": "someone@else.example"},
])
def test_the_saved_password_never_goes_to_a_new_server(client, offline_mail, changes):
    follow(client, save_mailbox(client, provider="other", smtp_host=SMTP_HOST, imap_host=IMAP_HOST))
    offline_mail.log.clear()
    offline_mail.imap_logins.clear()
    page = follow(client, save_mailbox(client, password="", **changes))
    assert "Type the password again: the server or username changed." in flashes(page)
    assert "Nothing was saved." in flashes(page)
    settings = config.get_settings()
    assert (settings.smtp_host, settings.imap_host, settings.smtp_username) == (SMTP_HOST, IMAP_HOST, FROM)
    assert offline_mail.log == [] and offline_mail.imap_logins == []  # no connection to anyone
    follow(client, post(client, "/mailbox", {"action": "test"}))
    assert all(entry[2] == SMTP_HOST for entry in offline_mail.log if entry[:2] == ("smtp", "connect"))
    # A separate reading password is kept only for its own reading server and username.
    follow(client, save_mailbox(client, password=PASSWORD, provider="other", smtp_host=SMTP_HOST, imap_host=IMAP_HOST,
                                imap_username="reader@acme-mail.example", imap_password="Reading-Pass-1"))
    page = follow(client, save_mailbox(client, password=PASSWORD, provider="other", smtp_host=SMTP_HOST,
                                       imap_host="imap.attacker.example", imap_username="reader@acme-mail.example"))
    assert "Type the reading password again" in flashes(page) and settings.imap_host == IMAP_HOST


def test_a_password_set_outside_openberry_needs_the_servers_changed_there_too(client, monkeypatch):
    follow(client, save_mailbox(client, provider="other", smtp_host=SMTP_HOST, imap_host=IMAP_HOST))
    monkeypatch.delitem(config._FROM_FILE, "OPENBERRY_SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("OPENBERRY_SMTP_PASSWORD", "env-pass")  # the server's environment (Docker)
    page = follow(client, save_mailbox(client, password="", provider="other", smtp_host=SMTP_HOST,
                                       imap_host="mail.attacker.example"))
    assert "The password is set outside OpenBerry (OPENBERRY_SMTP_PASSWORD)" in flashes(page)
    assert config.get_settings().imap_host == IMAP_HOST
    # Disconnect removes what OpenBerry saved, and says what it couldn't remove.
    page = follow(client, post(client, "/mailbox", {"action": "disconnect"}))
    assert "Some mailbox settings are set outside OpenBerry (OPENBERRY_SMTP_PASSWORD)" in flashes(page)
    assert "its password removed" not in flashes(page)


@pytest.mark.parametrize("changes,problem", [
    ({"from_email": "not an address"}, "Enter your email address"),
    ({"provider": "other", "smtp_host": "https://smtp.example.com/"}, "sending (SMTP) server"),
    ({"provider": "other", "smtp_host": "smtp.example.com", "imap_host": "imap example"}, "reading (IMAP) server"),
    ({"smtp_port": "25"}, "465 or 587"),
    ({"username": "sam rahman"}, "without spaces"),
    ({"password": ""}, "Enter the mailbox's password"),
    ({"password": "two\nlines"}, "can't contain line breaks"),
    # Read back from the .env file, the password would end at any of these, and the rest become another setting.
    *(({"password": f"Abc{sep}OPENBERRY_PUBLIC_REGISTRATION=true"}, "can't contain line breaks")
      for sep in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ")),
    ({"imap_username": "reader@acme-mail.example", "imap_password": "a b"}, "reading password can't contain"),
])
def test_bad_mailbox_settings_save_nothing(client, changes, problem):
    page = follow(client, save_mailbox(client, **changes))
    assert problem in flashes(page) and "Nothing was saved." in flashes(page)
    assert not env_file().exists() and config.get_settings().smtp_host == ""


def test_saving_the_mailbox_lifts_a_refused_login_but_never_a_providers_block(client, offline_mail):
    follow(client, save_mailbox(client))
    repo.record_email_failure(0, "", mailer.PROVIDER_BLOCK, "554 5.7.1 Message rejected as spam", FROM)
    page = follow(client, save_mailbox(client, password="", from_name="Sam R."))  # only the name changed
    state = repo.mailbox_state(FROM)
    assert state["paused"] and state["paused_until"] and "rejected as spam" in state["pause_reason"]
    assert "Now click Send test email" in flashes(page)
    repo.record_email_failure(0, "", mailer.AUTH, "535 refused", FROM)
    follow(client, save_mailbox(client, password=""))  # new settings: a refused login is tried again
    state = repo.mailbox_state(FROM)
    assert not state["auth_failed"] and state["paused_until"]  # the provider's block stays
    follow(client, post(client, "/mailbox", {"action": "retry"}))  # only Try again lifts it early
    assert not repo.mailbox_state(FROM)["paused"]


def test_settings_from_the_environment_win_and_are_read_only(client, monkeypatch):
    monkeypatch.setenv("OPENBERRY_SMTP_HOST", "smtp.from-env.example")
    config.get_settings().smtp_host = "smtp.from-env.example"
    page = client.get("/mailbox").text
    assert re.search(r'id="mb-smtp_host"[^>]*readonly', page) and "Set outside OpenBerry" in page
    follow(client, save_mailbox(client, provider="other", smtp_host="other.example", imap_host="imap.example.com"))
    assert config.get_settings().smtp_host == "smtp.from-env.example"
    assert "OPENBERRY_SMTP_HOST" not in env_file().read_text()


def test_the_test_email_goes_to_your_own_address_only_and_is_rate_limited(client, offline_mail):
    save_mailbox(client)
    page = follow(client, post(client, "/mailbox", {"action": "test", "to": "someone@else.example"}))
    assert f"Test email sent to {FROM}" in flashes(page) and "Inbox checked" in flashes(page)
    assert [s["to"] for s in offline_mail.sent] == [FROM]
    for _ in range(4):
        post(client, "/mailbox", {"action": "test"})
    page = follow(client, post(client, "/mailbox", {"action": "test"}))
    assert "You sent 5 test emails in the last 10 minutes" in flashes(page) and len(offline_mail.sent) == 5


def test_a_failed_test_email_says_why_in_plain_words(client, offline_mail):
    import smtplib

    save_mailbox(client)
    offline_mail.fail["login"] = smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")
    page = follow(client, post(client, "/mailbox", {"action": "test"}))
    assert "The test email wasn't sent: Your mailbox refused the username or password" in flashes(page)
    assert PASSWORD not in page


def test_check_inbox_retry_and_disconnect(client, company, offline_mail):
    save_mailbox(client)
    ready(company)
    repo.record_email_failure(0, "", mailer.AUTH, "refused", FROM)
    page = client.get("/mailbox").text
    assert "Sending and inbox checks are paused until you fix the mailbox settings" in html_lib.unescape(page)
    follow(client, post(client, "/mailbox", {"action": "retry"}))
    assert not repo.mailbox_state(FROM)["paused"]
    assert "Inbox checked" in flashes(follow(client, post(client, "/mailbox", {"action": "check"})))
    page = follow(client, post(client, "/mailbox", {"action": "disconnect"}))
    assert "Email sending is off for every company" in flashes(page)
    assert "OPENBERRY_SMTP_PASSWORD" not in env_file().read_text() and config.get_settings().smtp_password == ""
    assert repo.get_company(company.id).outreach.email_sending is False


def test_the_never_email_list_can_be_edited(client, company):
    lead = lead_with_email(company)
    repo.record_opt_out(lead, "email reply", "unsubscribe")
    repo.add_suppression("dead@bounced.example", "bounce", "5.1.1")
    page = html_lib.unescape(client.get("/mailbox").text)
    assert "lina@gulfbank.example" in page and "Opted out" in page and "Bounced" in page
    assert "Only remove it if this person asked to hear from you again" in page
    bounce = next(s for s in repo.list_suppressions() if s["reason"] == "bounce")
    follow(client, post(client, "/mailbox", {"action": "unsuppress", "suppression_id": str(bounce["id"])}))
    assert [s["email"] for s in repo.list_suppressions()] == ["lina@gulfbank.example"]


def test_every_email_form_needs_the_csrf_token(client, company):
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hi", channel="email", subject="Hi", status="approved")
    for path in ("/mailbox", f"/c/{company.id}/outreach/email", f"/c/{company.id}/outreach/email/resume",
                 f"/c/{company.id}/outreach/email/send-now", f"/c/{company.id}/messages/{msg.id}/email",
                 f"/c/{company.id}/leads/{lead}/unsubscribe"):
        assert client.post(path, data={"action": "on"}, follow_redirects=False).status_code == 403, path
    assert repo.get_company(company.id).outreach.email_sending is False


# --------------------------------------------------------------------------------------
# The Outreach page's Email sending card
# --------------------------------------------------------------------------------------


def test_the_card_is_off_and_folded_away_until_a_mailbox_is_connected(client, company):
    page = client.get(f"/c/{company.id}/outreach").text
    email_card = card(page)
    assert "Email sending (your mailbox)" in email_card and ">Off<" in email_card
    assert "<details class=\"email-setup\">" in email_card and "Connect your mailbox" in email_card
    text = html_lib.unescape(email_card)
    assert "Hostinger, for example, doesn't allow emails to people who didn't ask for them" in text
    assert "SPF, DKIM and DMARC" in text and "This isn't legal advice" in text
    assert "[your business address]" in text  # the default footer: the user must fill it in
    assert "turn on email sending so OpenBerry emails the ones you approve" in html_lib.unescape(page)


def card_post(client, company, **data: Any):
    form = {"email_daily_limit": "10", "email_gap_minutes": "5", "email_start_hour": "0", "email_end_hour": "24",
            "email_days": [str(d) for d in range(7)], "email_footer": FOOTER, **data}
    return post(client, f"/c/{company.id}/outreach/email", form, page=f"/c/{company.id}/outreach")


def test_turning_it_on_needs_a_mailbox_an_inbox_check_a_footer_and_the_acknowledgement(client, company):
    page = follow(client, card_post(client, company, action="on", acknowledge="true"))
    assert "Connect your mailbox first" in flashes(page)
    assert repo.get_company(company.id).outreach.email_sending is False
    connect_mailbox(config.get_settings())
    page = follow(client, card_post(client, company, action="on", acknowledge="true"))
    assert "Check your inbox on the Mailbox page first" in flashes(page)
    repo.save_inbox_check(FROM, True)
    page = follow(client, card_post(client, company, action="on", acknowledge="true",
                                    email_footer="Acme · [your business address]\nReply unsubscribe"))
    assert "Replace [your business address]" in flashes(page)
    page = follow(client, card_post(client, company, action="on", email_footer="Acme, Dubai"))
    assert 'with the word "unsubscribe"' in flashes(page) and "Tick the box" in flashes(page)
    assert repo.get_company(company.id).outreach.email_sending is False
    assert repo.get_company(company.id).outreach.email_footer == "Acme, Dubai"  # what they typed is kept
    page = follow(client, card_post(client, company, action="on", acknowledge="true"))
    assert "Email sending is on" in flashes(page) and "at most 10 a day, at least 5 minutes apart" in flashes(page)
    out = repo.get_company(company.id).outreach
    assert out.email_sending and out.email_daily_limit == 10 and out.email_footer == FOOTER.replace("\r\n", "\n")
    email_card = card(page)
    assert ">On<" in email_card and "Sends from <strong class=\"break\">Sam Rahman &lt;sam@acme-mail.example&gt;" in \
        email_card


def test_while_on_a_bad_footer_or_hours_are_never_saved_and_turning_off_always_works(client, company):
    ready(company)
    page = follow(client, card_post(client, company, action="save", email_footer="Acme Chauffeurs"))
    assert "Nothing was changed." in flashes(page) and repo.get_company(company.id).outreach.email_footer == FOOTER
    page = follow(client, card_post(client, company, action="save", email_start_hour="18", email_end_hour="9"))
    assert "The sending hours must end after they start." in flashes(page)
    page = follow(client, card_post(client, company, action="save", email_days=[]))
    assert "Choose at least one day" in flashes(page)
    page = follow(client, card_post(client, company, action="save", email_daily_limit="500"))
    assert "from 1 to 100" in flashes(page)
    resp = post(client, f"/c/{company.id}/outreach/email", {"action": "off"}, page=f"/c/{company.id}/outreach")
    assert "Email sending is off" in flashes(follow(client, resp))
    assert repo.get_company(company.id).outreach.email_sending is False


def test_auto_approved_emails_have_their_own_switch_shown_with_auto_approve(client, company):
    ready(company)
    assert "email_send_auto_approved" not in card(client.get(f"/c/{company.id}/outreach").text)
    repo.update_company(company.id, {"outreach": {"auto_approve": True}})
    email_card = card(client.get(f"/c/{company.id}/outreach").text)
    assert 'name="email_send_auto_approved"' in email_card and "data-confirm-check" in email_card
    page = follow(client, card_post(client, company, action="save", email_send_auto_approved="true"))
    assert "nobody reads them before they go out" in flashes(page)
    assert "Fully automatic:" in html_lib.unescape(card(page))


def test_the_card_lists_the_queue_what_is_held_back_and_what_needs_attention(client, company):
    ready(company)
    lina = lead_with_email(company)
    queued = repo.create_message(lina, "Hello Lina", channel="email", subject="Airport transfers", status="approved")
    noreply = lead_with_email(company, "noreply@hotel.example", "No Reply")
    held = repo.create_message(noreply, "Hi", channel="email", subject="Hi", status="approved")
    omar = lead_with_email(company, "omar@client.example", "Omar <b>Haddad</b>")
    unknown = repo.create_message(omar, "Hi Omar", channel="email", subject="Cars", status="approved")
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'unknown', send_error = 'The connection broke' WHERE id = ?",
                  (unknown.id,))
    email_card = card(client.get(f"/c/{company.id}/outreach").text)
    assert f"#msg-{queued.id}" in email_card and "Airport transfers · step 1" in email_card
    assert "Send now" in email_card
    assert "it is a no-reply or system address" in email_card and f"#msg-{held.id}" in email_card
    assert "May have been sent. Check your mailbox" in html_lib.unescape(email_card)
    assert 'value="mark_sent"' in email_card and 'value="send_again"' in email_card
    assert "Omar <b>Haddad</b>" not in email_card  # lead data is escaped
    page = client.get(f"/c/{company.id}/outreach?tab=approved").text
    item = re.search(rf'<li class="card queue-item" id="q-{queued.id}".*?</li>', page, re.S).group(0)
    assert "Queued for your mailbox" in item and "Added when sent" in item and "Bay Square" in item
    item = re.search(rf'<li class="card queue-item" id="q-{unknown.id}".*?</li>', page, re.S).group(0)
    assert "May have been sent" in item and "Added when sent" not in item


def test_the_may_have_been_sent_actions(client, company):
    ready(company)
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hi", channel="email", subject="Hi", status="approved")
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'unknown', email_claimed_at = ? WHERE id = ?",
                  (repo.iso(), msg.id))
    resp = post(client, f"/c/{company.id}/messages/{msg.id}/email", {"action": "send_again"},
                page=f"/c/{company.id}/outreach")
    assert "sent again" in flashes(follow(client, resp)) and repo.get_message(msg.id).email_state == ""
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'unknown' WHERE id = ?", (msg.id,))
    resp = post(client, f"/c/{company.id}/messages/{msg.id}/email", {"action": "mark_sent"},
                page=f"/c/{company.id}/outreach")
    follow(client, resp)
    stored = repo.get_message(msg.id)
    assert (stored.status, stored.sent_via) == ("sent", "smtp")
    sent_tab = client.get(f"/c/{company.id}/outreach?tab=sent").text
    assert "Sent from your mailbox" in sent_tab
    resp = post(client, f"/c/{company.id}/messages/{msg.id}/email", {"action": "retry"},
                page=f"/c/{company.id}/outreach")
    assert "can't be changed that way" in flashes(follow(client, resp))


def test_send_now_wakes_the_loop_or_sends_one_with_the_scheduler_off(client, company, offline_mail):
    ready(company)
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hello", channel="email", subject="Airport transfers", status="approved")
    page = follow(client, post(client, f"/c/{company.id}/outreach/email/send-now", {},
                               page=f"/c/{company.id}/outreach"))
    assert "1 email sent. Turn the scheduler on to send the rest automatically." in flashes(page)
    assert [s["to"] for s in offline_mail.sent] == ["lina@gulfbank.example"]
    assert repo.get_message(msg.id).sent_via == "smtp"
    page = follow(client, post(client, f"/c/{company.id}/outreach/email/send-now", {},
                               page=f"/c/{company.id}/outreach"))
    assert "Nothing was sent" in flashes(page) and len(offline_mail.sent) == 1
    import asyncio

    client.app.state.email_wake = asyncio.Event()
    page = follow(client, post(client, f"/c/{company.id}/outreach/email/send-now", {},
                               page=f"/c/{company.id}/outreach"))
    assert "Sending now" in flashes(page) and client.app.state.email_wake.is_set()


def test_approving_an_email_says_what_happens_and_needs_a_subject(client, company):
    lead = lead_with_email(company)
    draft = repo.create_message(lead, "Hello", channel="email", subject="", step=1)
    base = f"/c/{company.id}"
    resp = post(client, f"{base}/messages/{draft.id}", {"action": "approve", "next": f"{base}/outreach"},
                page=f"{base}/outreach")
    assert "Add a subject first" in flashes(follow(client, resp)) and repo.get_message(draft.id).status == "draft"
    resp = post(client, f"{base}/messages/{draft.id}", {"action": "approve", "subject": "Airport transfers",
                                                        "next": f"{base}/outreach"}, page=f"{base}/outreach")
    assert "Approved. Copy it and send it from your inbox." in flashes(follow(client, resp))
    ready(company)
    second = repo.create_message(lead, "Hello again", channel="email", subject="Cars", step=2)
    repo.update_message(draft.id, status="draft")
    resp = post(client, f"{base}/messages/{draft.id}", {"action": "approve", "next": f"{base}/outreach"},
                page=f"{base}/outreach")
    assert "OpenBerry will email it from your mailbox" in flashes(follow(client, resp))
    resp = post(client, f"{base}/messages/{second.id}", {"action": "approve", "next": f"{base}/outreach"},
                page=f"{base}/outreach")
    assert "It won't be emailed now (another email to this lead or its address goes first" in flashes(
        follow(client, resp))


def test_an_email_being_sent_cant_be_deleted_from_the_dashboard(client, company):
    ready(company)
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hello", channel="email", subject="Hi", status="approved")
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'data' WHERE id = ?", (msg.id,))
    base = f"/c/{company.id}"
    resp = post(client, f"{base}/messages/{msg.id}", {"action": "delete"}, page=f"{base}/outreach")
    assert "being sent right now" in flashes(follow(client, resp)) and repo.get_message(msg.id)


def test_the_bulk_approve_skips_emails_without_a_subject(client, company):
    lead = lead_with_email(company)
    draft = repo.create_message(lead, "Hello", channel="email", subject="")
    result = repo.bulk_update_drafts(company.id, [(draft.id, repo.message_version(draft))], "approve")
    assert result == {"done": [], "problems": {repo.BULK_NO_SUBJECT: 1}}


# --------------------------------------------------------------------------------------
# Lead page and dashboard
# --------------------------------------------------------------------------------------


def test_the_lead_page_marks_unsubscribed_and_shows_what_came_back(client, company):
    ready(company)
    lead = lead_with_email(company)
    draft = repo.create_message(lead, "Hello", channel="email", subject="Airport transfers")
    page = client.get(f"/c/{company.id}/leads/{lead}").text
    assert "Unsubscribed (never email again)" in page and "Added when sent" in page and "Bay Square" in page
    assert FOOTER in html_lib.unescape(page)
    resp = post(client, f"/c/{company.id}/leads/{lead}/unsubscribe", {}, page=f"/c/{company.id}/leads/{lead}")
    page = follow(client, resp)
    assert "OpenBerry never emails lina@gulfbank.example again" in flashes(page)
    found = repo.get_lead(lead)
    assert found.status == "disqualified" and "unsubscribed" in found.tags
    assert re.search(r"pill-email-problem[^>]*>.*?Unsubscribed</span>", page, re.S)  # the badge by the address
    assert "asked not to be emailed" in html_lib.unescape(page) and "What came back to your mailbox" in page
    assert repo.get_message(draft.id).auto_hold is True


def test_the_dashboard_says_email_sending_is_on_and_flags_problems(client, company):
    ready(company)
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hello", channel="email", subject="Hi", status="approved")
    page = re.sub(r"<[^>]+>", "", html_lib.unescape(client.get(f"/c/{company.id}").text))
    assert "Email sending is on: 0 of 30 emails sent in the last 24 hours · 1 queued." in page
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'failed', send_error = 'refused' WHERE id = ?", (msg.id,))
    page = re.sub(r"<[^>]+>", "", html_lib.unescape(client.get(f"/c/{company.id}").text))
    assert "1 email from your mailbox needs you: it may have been sent, or sending failed. Check it" in page
    settings_page = html_lib.unescape(client.get(f"/c/{company.id}/settings").text)
    assert "Email sending: On, at most 30 a day. Change it on the Outreach page." in re.sub(r"<[^>]+>", "",
                                                                                            settings_page)
    repo.save_inbox_check(FROM, False, "Your mailbox refused the username or password for reading mail.")
    page = re.sub(r"<[^>]+>", "", html_lib.unescape(client.get(f"/c/{company.id}").text))
    assert "Email sending is on, but OpenBerry can't read your inbox: 0 of 30 emails" in page
    assert "is inbox not checked" not in page
    assert ">Inbox problem<" in card(client.get(f"/c/{company.id}/outreach").text)


def test_the_card_speaks_to_you_on_your_clock_and_offers_only_what_can_work(client, company, monkeypatch):
    monkeypatch.setattr(repo, "EMAIL_LOCAL_TZ", timezone(timedelta(hours=4)))  # this computer is in Dubai
    ready(company)
    lina = lead_with_email(company)
    repo.create_message(lina, "Hello", channel="email", subject="Cars", status="sent")  # by hand, just now
    follow_up = repo.create_message(lina, "Following up", channel="email", subject="Cars", step=2, status="approved")
    page = html_lib.unescape(client.get(f"/c/{company.id}/outreach?tab=approved").text)
    item = re.search(rf'id="q-{follow_up.id}".*?</li>', page, re.S).group(0)
    line = re.search(r'<p class="email-line email-line-held">.*?</p>', item, re.S).group(0)
    assert "not due before" in line and "+00:00" not in line  # "Thu 15 Oct, 14:00", on this computer's clock
    with db.connect() as c:
        repo._save_email_pause(c, company.id, repo.utcnow() + timedelta(hours=2),
                               "3 hard bounces in 24 hours. Check the addresses before you resume.")
    text = re.sub(r"<[^>]+>", "", html_lib.unescape(card(client.get(f"/c/{company.id}/outreach").text)))
    assert "Only you can resume it early" in text and "the user" not in text
    assert "+00:00" not in text and ".)" not in text and ".." not in text
    repo.resume_email_sending(company.id)
    # A hard bounce: Try again can't work while the address is on the never-email list.
    omar = lead_with_email(company, "omar@client.example", "Omar Haddad")
    bounced = repo.create_message(omar, "Hi Omar", channel="email", subject="Cars", status="approved")
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'failed', email_to = 'omar@client.example', send_error = ? "
                  "WHERE id = ?", ("The address doesn't exist (the server said: 550 5.1.1 User unknown).", bounced.id))
    repo.add_suppression("omar@client.example", "bounce", "5.1.1 User unknown")
    email_card = card(client.get(f"/c/{company.id}/outreach").text)
    attention = re.search(r'<ul class="list email-attention">.*?</ul>', email_card, re.S).group(0)
    assert "Fix the address" in attention and 'value="retry"' not in attention
    resp = post(client, f"/c/{company.id}/messages/{bounced.id}/email", {"action": "retry"},
                page=f"/c/{company.id}/outreach")
    flashed = flashes(follow(client, resp))
    assert "It isn't emailed now: omar@client.example bounced" in flashed and "back in the queue" not in flashed
    # The lead page's never-email line starts with words, not a capitalized address.
    page = html_lib.unescape(client.get(f"/c/{company.id}/leads/{omar}").text)
    assert "This address bounced: it doesn't exist (" in page and "Omar@client.example" not in page


def test_the_confirmations_say_what_approving_and_turning_on_do(client, company):
    connect_mailbox(config.get_settings())
    repo.save_inbox_check(FROM, True)
    lina = lead_with_email(company)
    repo.create_message(lina, "Hello", channel="email", subject="Cars", status="approved")
    repo.create_message(lead_with_email(company, "noreply@hotel.example", "No Reply"), "Hi", channel="email",
                        subject="Hi", status="approved")  # held back: never emailed
    repo.create_message(lead_with_email(company, "omar@client.example", "Omar"), "Hi Omar", channel="email",
                        subject="Cars")  # a draft, so the Drafts tab has its bulk bar
    email_card = html_lib.unescape(card(client.get(f"/c/{company.id}/outreach").text))
    assert "starting with the 1 approved email ready now" in email_card and 'data-confirm-limit="#email-limit"' in \
        email_card
    assert "data-email-sending" not in client.get(f"/c/{company.id}/outreach?tab=drafts").text
    ready(company)
    assert "data-email-sending" in client.get(f"/c/{company.id}/outreach?tab=drafts").text


# --------------------------------------------------------------------------------------
# Claude (MCP) and the JSON API
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def mcp_client() -> AsyncIterator[Client]:
    async with Client(build_server()) as c:
        yield c


async def call(client: Client, tool: str, **args: Any) -> tuple[bool, Any]:
    result = await client.call_tool(tool, args)
    return result.is_error, (result.content[0].text if result.is_error else result.structured_content)


async def test_get_email_status_never_shows_the_mailbox_credentials(company, web_settings):
    ready(company)
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hello", channel="email", subject="Airport transfers", status="approved")
    async with mcp_client() as c:
        error, status = await call(c, "get_email_status", company_id=company.id)
    assert not error
    assert status["state"] == "on" and status["enabled"] and status["queued"] == 1
    assert status["next"][0]["message_id"] == msg.id and status["mailbox"]["from"] == FROM
    assert status["mailbox"]["inbox_ok"] is True and status["footer"] == FOOTER
    dumped = json.dumps(status)
    for secret in (SMTP_HOST, IMAP_HOST, "app-password-1", "smtp_", "imap_", "password", "username", "465", "993"):
        assert secret not in dumped, secret


async def test_get_email_status_never_names_the_mail_servers_in_errors(company, web_settings, offline_mail):
    import socket

    ready(company)
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hello", channel="email", subject="Airport transfers", status="approved")
    offline_mail.imap_fail["connect"] = socket.gaierror(-2, "Name or service not known")
    assert services.check_inbox()["status"] == "failed"  # "Can't find the server imap.acme-mail.example..."
    repo.record_email_failure(0, "", mailer.CONFIG, f"A secure connection to {SMTP_HOST} failed.", FROM)
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'failed', send_error = ? WHERE id = ?",
                  (f"Your mail server {SMTP_HOST.upper()} refused this email", msg.id))
    async with mcp_client() as c:
        error, status = await call(c, "get_email_status", company_id=company.id)
    assert not error and status["mailbox"]["inbox_error"].startswith("Can't find the server your mail server")
    dumped = json.dumps(status).lower()
    assert SMTP_HOST not in dumped and IMAP_HOST not in dumped
    with TestClient(create_app(web_settings)) as api:  # the JSON API returns the same
        assert IMAP_HOST not in api.get(f"/api/companies/{company.id}/email-status").text


async def test_claude_may_only_make_email_sending_safer(company, web_settings):
    ready(company, email_daily_limit=20, email_send_auto_approved=True, email_gap_minutes=10)
    async with mcp_client() as c:
        for change, words in (({"email_daily_limit": 50}, "raise the daily email limit"),
                              ({"email_gap_minutes": 6}, "send emails closer together"),
                              ({"email_start_hour": 0, "email_end_hour": 24, "email_days": [0]}, None),
                              ({"email_footer": "Other footer, reply unsubscribe"}, "change the email footer"),
                              ({"email_paused_until": None, "email_pause_reason": "x"}, "pause or resume")):
            error, text = await call(c, "update_company", company_id=company.id, changes={"outreach": change})
            if words is None:
                assert not error, text  # the same hours on one day: narrower
            else:
                assert error and words in text and "only the user can" in text, text
        error, _ = await call(c, "update_company", company_id=company.id,
                              changes={"outreach": {"email_daily_limit": 5, "email_gap_minutes": 30,
                                                    "email_send_auto_approved": False, "email_sending": False}})
        assert not error
        error, text = await call(c, "update_company", company_id=company.id, changes={"outreach": {
            "email_sending": True}})
        assert error and "turn email sending on" in text
        error, text = await call(c, "register_company", profile={"name": "New Co", "outreach": {
            "email_sending": True, "email_footer": "x unsubscribe"}})
        assert error and "turn email sending on" in text
    out = repo.get_company(company.id).outreach
    assert (out.email_sending, out.email_daily_limit, out.email_gap_minutes, out.email_send_auto_approved) == (
        False, 5, 30, False)


async def test_claude_logs_an_opt_out_and_sees_the_email_fields(company, web_settings):
    ready(company)
    lead = lead_with_email(company)
    msg = repo.create_message(lead, "Hello", channel="email", subject="Hi", status="approved")
    async with mcp_client() as c:
        error, context = await call(c, "get_outreach_context", lead_id=lead, channel="email", step=2)
        assert not error and context["limits"]["email_sending"] is True
        assert context["limits"]["footer_added_when_sent"] == FOOTER
        error, out = await call(c, "log_reply", lead_id=lead, body="Please remove me from your list", channel="email",
                                opt_out=True)
        assert not error and out["opted_out"] is True
        error, context = await call(c, "get_outreach_context", lead_id=lead, channel="email", step=2)
        assert any("Never email this lead" in w for w in context["warnings"])
        error, listed = await call(c, "list_outreach", company_id=company.id)
    row = next(m for m in listed["messages"] if m["id"] == msg.id)
    assert {"email_state", "send_error", "sent_via"} <= set(row)
    assert repo.get_lead(lead).status == "disqualified"
    assert repo.suppressed_addresses(["lina@gulfbank.example"])["lina@gulfbank.example"]["reason"] == "opt_out"


async def test_saving_an_email_draft_says_openberry_will_email_it_once_approved(company, web_settings):
    ready(company)
    lead = lead_with_email(company)
    async with mcp_client() as c:
        error, out = await call(c, "save_outreach_message", lead_id=lead, channel="email", subject="Cars",
                                body="Hi Lina, we drive executives in Dubai.")
    assert not error and "OpenBerry emails it from their mailbox" in out["reminder"]


def test_the_mcp_server_never_sends_or_reads_mail():
    import inspect

    source = inspect.getsource(mcp_server)
    for forbidden in ("send_next_email", "check_inbox", "mailer.send", "send_test", "Inbox(", "save_inbox_check",
                      "smtp_password", "imap_password"):
        assert forbidden not in source, forbidden


def test_the_api_reads_the_email_status_and_guards_the_settings(client, company):
    ready(company)
    resp = client.get(f"/api/companies/{company.id}/email-status")
    assert resp.status_code == 200 and resp.json()["state"] == "on" and "app-password-1" not in resp.text
    assert SMTP_HOST not in resp.text
    for change in ({"email_daily_limit": 100}, {"email_footer": "x unsubscribe"},
                   {"email_send_auto_approved": True}):
        resp = client.patch(f"/api/companies/{company.id}", json={"outreach": change})
        assert resp.status_code == 403 and "only the user can" in resp.json()["detail"]
    assert client.patch(f"/api/companies/{company.id}", json={"outreach": {"email_sending": False}}).status_code == 200
    resp = client.patch(f"/api/companies/{company.id}", json={"outreach": {"email_sending": True}})
    assert resp.status_code == 403
    resp = client.post("/api/companies", json={"name": "Api Co", "outreach": {"email_sending": True}})
    assert resp.status_code == 403
    assert client.post(f"/api/companies/{company.id}/email/send", json={}).status_code in (404, 405)


def test_public_registrations_never_turn_email_sending_on():
    from openberry.web.pages import as_pending_review

    data = CompanyIn.model_validate({"name": "Public Co", "outreach": {
        "email_sending": True, "email_daily_limit": 100, "email_footer": "x unsubscribe",
        "email_send_auto_approved": True}})
    out = as_pending_review(data).outreach
    assert (out.email_sending, out.email_daily_limit, out.email_footer, out.email_send_auto_approved) == (
        False, 30, "", False)


# --------------------------------------------------------------------------------------
# Docs
# --------------------------------------------------------------------------------------


def test_the_email_docs_give_the_rules_the_limits_and_their_sources():
    root = Path(__file__).resolve().parent.parent
    doc = (root / "docs" / "EMAIL_SENDING.md").read_text(encoding="utf-8")
    for needed in ("Hostinger", "opt in", "Zoho", "smtppro.zoho.com", "smtp.zoho.com", "imap.zoho.com",
                   "imappro.zoho.com",
                   "smtp.hostinger.com", "imap.hostinger.com", "app password", "SPF", "DKIM", "DMARC",
                   "separate", "30", "100", "5 minutes", "09:00", "unsubscribe", "PDPL", "GDPR", "CAN-SPAM",
                   "not legal advice", "support.hostinger.com", "zoho.com", "ftc.gov"):
        assert needed in doc, needed
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "OpenBerry never sends email itself" not in readme and "docs/EMAIL_SENDING.md" in readme
    agent_doc = (root / "docs" / "AI_AGENT_SENDING.md").read_text(encoding="utf-8")
    assert "EMAIL_SENDING.md" in agent_doc
