"""Email sending pages in a real browser (app.js and app.css): what only a browser shows. Round 2 of the review.

Skipped when Playwright or a Chromium for it isn't installed. The dashboard runs in a thread with the test's own
database (test_keep_scroll_browser's fixtures); no mail server is reached.
"""

from __future__ import annotations

from typing import Any

import pytest

from openberry import db, repo
from openberry.models import LeadIn

pytest.importorskip("playwright.sync_api")

from test_keep_scroll_browser import DESKTOP, base_url, browser  # noqa: E402,F401  (fixtures)


def logged_in_page(browser: Any, base_url: str, viewport: dict[str, int] = DESKTOP) -> Any:
    page = browser.new_page(viewport=viewport)
    page.goto(base_url + "/")
    return page


def test_another_mail_provider_starts_with_empty_servers(browser, base_url):
    """R2UI-12c: choosing "Another mail provider" after a preset clears the preset's servers, which would be wrong."""
    page = logged_in_page(browser, base_url)
    page.goto(base_url + "/mailbox")
    page.select_option("select[name=provider]", "zoho")
    assert "zoho" in page.input_value("[data-mail-smtp]") and "zoho" in page.input_value("[data-mail-imap]")
    page.select_option("select[name=provider]", "other")
    assert page.input_value("[data-mail-smtp]") == "" and page.input_value("[data-mail-imap]") == ""


def bounced_lead(company_id: int) -> int:
    lead, _ = repo.upsert_lead(company_id, LeadIn(full_name="Omar Haddad", lead_company="Old Creek Hotel",
                                                  email="sales@oldcreek-hotel.example"))
    msg = repo.create_message(lead.id, "Hi Omar,\n\nWe drive your guests to and from DXB.", channel="email",
                              subject="Airport transfers", status="approved")
    detail = ("Bounced: the address doesn't work. Their mail server sent it back: 5.1.1 smtp; 550 5.1.1 "
              "<sales@oldcreek-hotel.example>: Recipient address rejected: User unknown in virtual mailbox table "
              "(in reply to RCPT TO command)")
    with db.connect() as c:
        c.execute("UPDATE messages SET email_state = 'failed', email_to = 'sales@oldcreek-hotel.example', "
                  "send_error = ? WHERE id = ?", (detail, msg.id))
        c.execute("INSERT INTO email_events (company_id, lead_id, message_id, kind, detail, created_at) "
                  "VALUES (?, ?, ?, 'bounce', ?, ?)", (company_id, lead.id, msg.id, detail, repo.iso()))
    repo.add_suppression("sales@oldcreek-hotel.example", "bounce", "5.1.1 User unknown")
    return lead.id


def test_fix_the_address_opens_the_profile_form(browser, base_url, company):
    """R2UI-4: following Fix the address opens the Edit profile details (the email field is in it), in view."""
    lead = bounced_lead(company.id)
    page = logged_in_page(browser, base_url)
    page.goto(f"{base_url}/c/{company.id}/leads/{lead}#edit-profile")
    page.wait_for_function("document.getElementById('edit-profile').open")
    assert page.is_visible("#edit-profile input[name=email]")
    page.wait_for_function("Math.abs(document.getElementById('edit-profile').getBoundingClientRect().top) < 120")
    # The same link on the lead page itself only changes the hash (no reload): it opens the form too.
    page.goto(f"{base_url}/c/{company.id}/leads/{lead}")
    assert not page.evaluate("document.getElementById('edit-profile').open")
    page.click("a:has-text('Fix the address')")
    page.locator("#edit-profile[open]").wait_for()


def test_what_came_back_stays_inside_the_outreach_card(browser, base_url, company):
    """R2UI-8: a long bounce detail wraps inside the lead page's Outreach card on a desktop screen."""
    lead = bounced_lead(company.id)
    page = logged_in_page(browser, base_url)
    page.goto(f"{base_url}/c/{company.id}/leads/{lead}")
    page.click(".email-events > summary")
    card_right = page.evaluate("document.getElementById('outreach').getBoundingClientRect().right")
    rows = page.evaluate("[...document.querySelectorAll('.email-events .list-item *')]"
                         ".map(e => e.getBoundingClientRect().right)")
    assert rows and max(rows) <= card_right + 0.5
    turn = page.evaluate("getComputedStyle(document.querySelector('.email-events > summary .icon')).transform")
    assert turn not in ("none", "")  # the chevron shows the list open
