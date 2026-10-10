"""Keep-your-place on the Outreach page, in a real browser (app.js initKeepScroll).

Skipped when Playwright or a Chromium for it isn't installed. The dashboard runs in a thread with the test's own
database; the browser clicks and types like a user.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from openberry import db, repo
from openberry.models import LeadIn

sync_api = pytest.importorskip("playwright.sync_api")

DESKTOP = {"width": 1280, "height": 860}
PHONE = {"width": 390, "height": 844}


@pytest.fixture
def base_url(settings) -> Iterator[str]:
    import uvicorn

    from openberry.web.app import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


@pytest.fixture
def browser() -> Iterator[Any]:
    """Playwright's own Chromium, else any Chromium in PLAYWRIGHT_BROWSERS_PATH; skipped without one."""
    candidates: list[str | None] = [None]
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root:
        candidates += sorted(str(p) for p in Path(root).glob("chromium-*/chrome-*/chrome"))
    with sync_api.sync_playwright() as pw:
        found = None
        for executable in candidates:
            try:
                found = pw.chromium.launch(executable_path=executable)
                break
            except sync_api.Error:
                continue
        if found is None:
            pytest.skip("no Chromium for Playwright")
        try:
            yield found
        finally:
            found.close()


def stamp(message_id: int, created_hours_ago: float, updated_hours_ago: float) -> None:
    now = repo.utcnow()
    with db.connect() as c:
        c.execute("UPDATE messages SET created_at = ?, updated_at = ? WHERE id = ?",
                  (repo.iso(now - timedelta(hours=created_hours_ago)),
                   repo.iso(now - timedelta(hours=updated_hours_ago)), message_id))


def drafts(company_id: int, names: list[str]) -> list[int]:
    ids = []
    for name in names:
        slug = name.lower().replace(" ", "-")
        lead, _ = repo.upsert_lead(company_id, LeadIn(full_name=name, lead_company="Northwind",
                                                      linkedin_url=f"https://www.linkedin.com/in/{slug}"))
        ids.append(repo.create_message(lead.id, f"Hi {name.split()[0]}, saw your post about airport transfers in "
                                                "Dubai.\n\nHappy to share how we handle them for teams like yours.",
                                       channel="linkedin_dm").id)
    return ids


def outreach_with_cards_coming_due(company_id: int) -> tuple[list[int], list[int]]:
    """Auto-approve on (2 h). Two newer drafts at the top of the Drafts tab wait one more hour; eight older ones
    below were edited just now. Returns (newer, older) message ids, in the order the tab lists them."""
    repo.update_company(company_id, {"outreach": {"auto_approve": True, "auto_approve_hours": 2,
                                                  "auto_approve_since": repo.iso(repo.utcnow() - timedelta(days=30))}})
    newer = drafts(company_id, ["Mei Tan", "Hamza Ali"])
    for i, msg_id in enumerate(newer):
        stamp(msg_id, 1 + i * 0.01, 1)
    older = drafts(company_id, [f"Lead {n}" for n in range(8)])
    for i, msg_id in enumerate(older):
        stamp(msg_id, 3 + i, 0)
    return newer, older


def card_top(page: Any, message_id: int) -> float:
    return page.evaluate("id => document.getElementById(id).getBoundingClientRect().top", f"q-{message_id}")


def scroll_card_to(page: Any, message_id: int, y: int) -> None:
    page.evaluate("([id, y]) => { const el = document.getElementById(id); "
                  "window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - y); }", [f"q-{message_id}", y])
    assert abs(card_top(page, message_id) - y) < 1


def come_due(message_ids: list[int]) -> None:
    """While the user reads, the newer drafts' review window passes: the next page load approves them."""
    for msg_id in message_ids:
        stamp(msg_id, 3, 3)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_hold_keeps_the_card_in_place_when_drafts_above_it_were_approved_meanwhile(company, base_url, browser,
                                                                                   viewport):
    newer, older = outreach_with_cards_coming_due(company.id)
    target = older[2]
    page = browser.new_page(viewport=viewport)
    page.goto(f"{base_url}/c/{company.id}/outreach?tab=drafts")
    scroll_card_to(page, target, 300)
    come_due(newer)
    with page.expect_navigation():
        page.click(f"#q-{target} .auto-line button[value=hold]")
    page.wait_for_load_state("load")
    assert [repo.get_message(m).status for m in newer] == ["approved", "approved"]  # gone from the tab
    assert repo.get_message(target).auto_hold
    assert page.locator(f"#q-{target} .auto-line button[value=release]").count() == 1
    assert abs(card_top(page, target) - 300) <= 2  # the card the user held is where it was

    # Approve takes the card off the tab: the next card takes its place, also when cards above it left.
    target, after = older[4], older[5]
    scroll_card_to(page, target, 300)
    come_due([older[0]])  # a card above it comes due meanwhile
    with page.expect_navigation():
        page.click(f"#q-{target} button[value=approve]")
    page.wait_for_load_state("load")
    assert repo.get_message(target).status == "approved" and repo.get_message(older[0]).status == "approved"
    assert abs(card_top(page, after) - 300) <= 2

    # Approving the last card, with the tabs far above the screen: the cards left are on screen, not an empty gap.
    last, before_last = older[-1], older[-2]
    page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    with page.expect_navigation():
        page.click(f"#q-{last} button[value=approve]")
    page.wait_for_load_state("load")
    assert repo.get_message(last).status == "approved"
    assert 0 < card_top(page, before_last) < viewport["height"]
    page.close()


def test_hold_and_release_from_the_keyboard_keep_the_focus_on_the_same_draft(company, base_url, browser):
    _, older = outreach_with_cards_coming_due(company.id)
    target = older[3]
    page = browser.new_page(viewport=DESKTOP)
    page.goto(f"{base_url}/c/{company.id}/outreach?tab=drafts")
    scroll_card_to(page, target, 300)
    page.focus(f"#q-{target} a.lead-name")

    def focused() -> list[Any]:
        return page.evaluate("() => { const el = document.activeElement; const li = el && el.closest('.queue > li');"
                             " return [el && el.value, li && li.id]; }")

    for _ in range(10):
        page.keyboard.press("Tab")
        if focused() == ["hold", f"q-{target}"]:
            break
    else:
        pytest.fail(f"Tab never reached the Hold button of draft {target}: {focused()}")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    page.wait_for_load_state("load")
    assert repo.get_message(target).auto_hold
    # The button now in Hold's place, "Let it auto-approve", has the focus: the next Enter acts on the same draft.
    assert focused() == ["release", f"q-{target}"]
    with page.expect_navigation():
        page.keyboard.press("Enter")
    page.wait_for_load_state("load")
    assert not repo.get_message(target).auto_hold
    assert focused() == ["hold", f"q-{target}"]
    page.close()
