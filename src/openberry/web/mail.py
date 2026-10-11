"""Email sending in the dashboard: the Mailbox page (install-level: the user's own mailbox, saved to the data folder's
.env file through envfile.py) and the Outreach page's Email sending card, plus the email actions on messages and leads.

Logged-in dashboard users only (in local mode: this machine), every form CSRF-protected. Passwords are write-only:
never put in the page, a flash, a log line or an error message. The JSON API and the MCP server can't read or set
any of this, and can't turn email sending on, raise its limits, change its hours or footer, or resume it.
"""

from __future__ import annotations

import re
from datetime import timedelta
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from .. import config, envfile, mailer, repo, services
from ..models import APPROVED_VIA_AUTO, WEEKDAYS, Company, Message
from ..outreach import default_footer, footer_problems
from . import forms
from .auth import checked_form, require_login
from .ratelimit import limits
from .session import flash, safe_next
from .ui import local_dt, render, to_datetime

router = APIRouter(dependencies=[Depends(require_login)], include_in_schema=False)

MAILBOX_BACK = "/mailbox"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

# The Mailbox page's presets: they fill the servers and ports, the user can still change them. Zoho's hosts end in
# the account's data centre ({region}: com, eu, in, com.au).
PROVIDERS: dict[str, dict[str, Any]] = {
    "zoho": {
        "label": "Zoho Mail, paid plan (your own domain)", "smtp": "smtppro.zoho.{region}", "port": 465,
        "imap": "imappro.zoho.{region}", "save_sent": True,
        "help": "Username: your full email address. If your Zoho account has two-step sign-in, create an "
                "app-specific password in Zoho (My Account, Security, App passwords). Turn on IMAP in Zoho Mail: "
                "Settings, Mail Accounts, IMAP Access. That page shows your servers: if it says imap.zoho.com, "
                "type that as the reading server.",
        "note": "Zoho Mail is made for one-to-one business email: it doesn't allow bulk or burst sending, and limits "
                "outgoing email to between 50 and 500 an hour depending on your sending reputation. OpenBerry sends "
                "one email at a time, minutes apart.",
    },
    "zoho_personal": {
        "label": "Zoho Mail, personal or free plan", "smtp": "smtp.zoho.{region}", "port": 587,
        "imap": "imap.zoho.{region}", "save_sent": True,
        "help": "Username: your full email address. Zoho's free plan for organizations may not include IMAP and "
                "SMTP access, which OpenBerry needs. Turn on IMAP in Zoho Mail: Settings, Mail Accounts, IMAP Access.",
        "note": "Zoho Mail is made for one-to-one business email: it doesn't allow bulk or burst sending.",
    },
    "hostinger": {
        "label": "Hostinger Email", "smtp": "smtp.hostinger.com", "port": 465, "imap": "imap.hostinger.com",
        "save_sent": True,
        "help": "Username: your full email address. The servers are listed in hPanel: Emails, Connect apps & "
                "devices.",
        "warning": "Hostinger doesn't allow emails to people who didn't ask for them (opt-in), and can block a "
                   "mailbox that sends them. Keep Hostinger for your clients, and use another mailbox, ideally on "
                   "another domain, for outreach.",
    },
    "gmail": {
        "label": "Gmail or Google Workspace (app password)", "smtp": "smtp.gmail.com", "port": 465,
        "imap": "imap.gmail.com", "save_sent": False,
        "help": "Turn on two-step verification, then create an app password (Google Account, Security, App "
                "passwords) and paste it as the password. Gmail saves sent emails itself, so leave Save a copy off.",
    },
    "other": {"label": "Another mail provider", "smtp": "", "port": 465, "imap": "", "save_sent": True,
              "help": "Your provider's help pages list its SMTP (sending) and IMAP (reading) servers. OpenBerry uses "
                      "SMTP on port 465 or 587 and IMAP on port 993, always encrypted."},
}
ZOHO_REGIONS = (("com", "zoho.com"), ("eu", "zoho.eu (Europe)"), ("in", "zoho.in (India)"),
                ("com.au", "zoho.com.au (Australia)"))
_HOST = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
                   r"[A-Za-z0-9])?)+")
FIELDS = {  # form field -> .env name
    "provider": "OPENBERRY_MAIL_PROVIDER", "smtp_host": "OPENBERRY_SMTP_HOST", "smtp_port": "OPENBERRY_SMTP_PORT",
    "username": "OPENBERRY_SMTP_USERNAME", "password": "OPENBERRY_SMTP_PASSWORD",
    "from_email": "OPENBERRY_SMTP_FROM_EMAIL", "from_name": "OPENBERRY_SMTP_FROM_NAME",
    "imap_host": "OPENBERRY_IMAP_HOST", "imap_username": "OPENBERRY_IMAP_USERNAME",
    "imap_password": "OPENBERRY_IMAP_PASSWORD", "save_sent": "OPENBERRY_IMAP_SAVE_SENT",
}
SECRET_FIELDS = ("password", "imap_password")


def preset_hosts(provider: str, region: str = "com") -> tuple[str, int, str]:
    preset = PROVIDERS.get(provider) or PROVIDERS["other"]
    region = region if region in dict(ZOHO_REGIONS) else "com"
    return preset["smtp"].format(region=region), preset["port"], preset["imap"].format(region=region)


def zoho_region(host: str) -> str:
    match = re.search(r"\.zoho\.(com\.au|com|eu|in)$", host or "")
    return match.group(1) if match else "com"


def _back() -> RedirectResponse:
    return RedirectResponse(MAILBOX_BACK, status_code=303)


def _plain_http_elsewhere(request: Request) -> bool:
    """The page came over plain HTTP from another computer: a password typed here travels unencrypted."""
    return request.url.scheme == "http" and (request.url.hostname or "") not in LOCAL_HOSTS


def mailbox_context(request: Request) -> dict[str, Any]:
    config.refresh_saved_settings()
    settings = config.get_settings()
    states = {field: envfile.setting_state(name) for field, name in FIELDS.items()}
    provider = settings.mail_provider if settings.mail_provider in PROVIDERS else ("other" if settings.smtp_host
                                                                                  else "zoho")
    address = settings.mailbox_address()
    return {
        "active": "mailbox", "title": "Mailbox", "env_path": str(config.settings_file()),
        "providers": PROVIDERS, "zoho_regions": ZOHO_REGIONS, "region": zoho_region(settings.smtp_host),
        "provider": provider, "states": states,
        "locked": {field: state.source in ("environment", "file") for field, state in states.items()},
        "values": {"smtp_host": settings.smtp_host, "smtp_port": settings.smtp_port,
                   "username": settings.smtp_username, "from_email": settings.smtp_from_email or settings.smtp_username,
                   "from_name": settings.smtp_from_name, "imap_host": settings.imap_host,
                   "imap_username": settings.imap_username, "save_sent": settings.imap_save_sent},
        "password_saved": bool(settings.smtp_password), "imap_password_saved": bool(settings.imap_password),
        "connected": bool(address), "address": address,
        "mailbox": repo.mailbox_state(address) if address else None,
        "plain_http": _plain_http_elsewhere(request),
        "suppressions": repo.list_suppressions(limit=200),
        "sending_stopped": services.sending_stopped(),
        "presets_json": {key: {"smtp": p["smtp"], "port": p["port"], "imap": p["imap"], "save_sent": p["save_sent"]}
                         for key, p in PROVIDERS.items()},
    }


@router.get("/mailbox")
def mailbox_page(request: Request) -> Response:
    return render(request, "mailbox.html", mailbox_context(request))


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _mailbox_changes(form: FormData) -> tuple[dict[str, str | None], list[str]]:
    """The settings to save from the Mailbox form, and what is wrong with it (nothing is saved then)."""
    settings = config.get_settings()
    problems: list[str] = []
    provider = _text(form, "provider")
    if provider not in PROVIDERS:
        provider = "other"
    smtp_default, port_default, imap_default = preset_hosts(provider, _text(form, "region"))
    from_email = _text(form, "from_email")
    username = _text(form, "username") or from_email
    smtp_host = (_text(form, "smtp_host") or smtp_default).lower()
    imap_host = (_text(form, "imap_host") or imap_default).lower()
    port_text = _text(form, "smtp_port") or str(port_default)
    from_name = " ".join(_text(form, "from_name").split())
    password = form.get("password") if isinstance(form.get("password"), str) else ""
    imap_password = form.get("imap_password") if isinstance(form.get("imap_password"), str) else ""
    if not mailer.valid_address(from_email):
        problems.append("Enter your email address, like sam@yourcompany.com.")
    if not _HOST.fullmatch(smtp_host):
        problems.append("Enter the sending (SMTP) server's name, like smtppro.zoho.com, without https:// or a port.")
    if not _HOST.fullmatch(imap_host):
        problems.append("Enter the reading (IMAP) server's name, like imap.zoho.com, without https:// or a port.")
    if port_text not in ("465", "587"):
        problems.append("The sending port must be 465 or 587. OpenBerry always uses an encrypted connection.")
    if not username or len(username) > 254 or any(ch.isspace() for ch in username):
        problems.append("Enter the username without spaces (usually your full email address).")
    if len(from_name) > 100:
        problems.append("Keep the name under 100 characters.")
    for label, secret in (("password", password), ("reading password", imap_password)):
        if envfile.breaks_lines(secret):
            problems.append(f"The {label} can't contain line breaks.")
    if not password and not settings.smtp_password:
        problems.append("Enter the mailbox's password (or app password).")
    imap_username = _text(form, "imap_username")
    if imap_username and any(ch.isspace() for ch in imap_username):
        problems.append("Enter the reading username without spaces.")
    problems += _password_moves(settings, smtp_host, username, imap_host, imap_username, password, imap_password)
    changes: dict[str, str | None] = {
        FIELDS["provider"]: provider, FIELDS["smtp_host"]: smtp_host, FIELDS["smtp_port"]: port_text,
        FIELDS["username"]: username, FIELDS["from_email"]: from_email, FIELDS["from_name"]: from_name or None,
        FIELDS["imap_host"]: imap_host, FIELDS["imap_username"]: imap_username or None,
        FIELDS["save_sent"]: "true" if forms.checked(form, "save_sent") else "false",
    }
    if password:  # an empty password field keeps the saved one
        changes[FIELDS["password"]] = password
    if imap_password:
        changes[FIELDS["imap_password"]] = imap_password
    elif not imap_username:
        changes[FIELDS["imap_password"]] = None  # the same details for reading and sending
    # Settings set outside OpenBerry (the environment or another .env file) win and stay as they are.
    return {k: v for k, v in changes.items() if _editable(k)}, problems


def _password_moves(settings: Any, smtp_host: str, username: str, imap_host: str, imap_username: str, password: str,
                    imap_password: str) -> list[str]:
    """A saved password only ever goes to the servers and username it was typed for. Kept for a new server, anyone
    at this dashboard could send it to a server of theirs and read it there, though the page never shows it. So a
    changed server or username needs the password typed again (a port change alone doesn't, nor filling in a server
    for the first time next to a password set outside OpenBerry)."""
    def changed(field: str, typed: str, saved: str) -> bool:
        saved = (saved or "").strip().lower()
        return _editable(FIELDS[field]) and bool(saved) and typed.strip().lower() != saved

    smtp_moved = changed("smtp_host", smtp_host, settings.smtp_host) or changed("username", username,
                                                                                settings.smtp_username)
    imap_moved = changed("imap_host", imap_host, settings.imap_host) or changed("imap_username", imap_username,
                                                                                settings.imap_username)
    keeps_imap = bool(imap_username and settings.imap_password and not imap_password)
    reads_with_main = not (imap_password or keeps_imap)  # no reading password of its own: the main one reads too
    problems = []
    if settings.smtp_password and (smtp_moved or (imap_moved and reads_with_main)):
        if not _editable(FIELDS["password"]):
            problems.append(f"The password is set outside OpenBerry ({FIELDS['password']}): change the servers and "
                            "username there too.")
        elif not password:
            problems.append("Type the password again: the server or username changed.")
    if keeps_imap and imap_moved:
        if not _editable(FIELDS["imap_password"]):
            problems.append(f"The reading password is set outside OpenBerry ({FIELDS['imap_password']}): change the "
                            "reading server and username there too.")
        else:
            problems.append("Type the reading password again: the reading server or username changed.")
    return problems


def _editable(name: str) -> bool:
    """The dashboard may change this setting: it isn't set in the environment or in a .env file read first."""
    source, where = config.setting_source(name)
    return source == "" or (source == "file" and where == config._resolved(config.settings_file()))


def _save(request: Request, changes: dict[str, str | None]) -> bool:
    try:
        envfile.save_settings(changes)
    except envfile.SettingNotSaved as exc:
        flash(request, str(exc), "error")
        return False
    except OSError as exc:
        flash(request, f"Couldn't save the mailbox settings: {exc.strerror or type(exc).__name__}.", "error")
        return False
    return True


def _inbox_flash(request: Request, result: dict[str, Any], before: str = "") -> None:
    if result["status"] == "ok":
        flash(request, f"{before}Inbox checked: OpenBerry can read it.".strip())
    elif result["status"] == "failed":
        flash(request, f"{before}OpenBerry couldn't read your inbox: {result['text']}".strip(), "error")
    else:
        flash(request, f"{before}Connect your mailbox first: fill in every field and save.".strip(), "error")


@router.post("/mailbox")
async def mailbox_save(request: Request, form: FormData = Depends(checked_form)) -> Response:
    """action = save | test | check | retry | disconnect | unsuppress. Always redirects to /mailbox with a flash.

    The passwords never go into a flash, a log line, an error message or the page.
    """
    action = _text(form, "action")
    if action == "save":
        changes, problems = _mailbox_changes(form)
        if problems:
            for problem in problems:
                flash(request, problem, "error")
            flash(request, "Nothing was saved.", "error")
            return _back()
        if not await run_in_threadpool(_save, request, changes):
            return _back()
        # New settings: a refused login is tried again at once. A provider's block (24 hours, to protect the
        # mailbox from suspension) and the waits after failures stay: only Try again lifts them early.
        await run_in_threadpool(partial(repo.retry_mailbox, auth_only=True))
        result = await run_in_threadpool(services.check_inbox)
        _inbox_flash(request, result, "Mailbox saved. ")
        if result["status"] == "ok":
            flash(request, "Now click Send test email to check that your mailbox can send.", "info")
    elif action == "test":
        if wait := limits(request).test_email.allow("mailbox"):
            flash(request, f"You sent 5 test emails in the last 10 minutes. Try again in {int(wait // 60) + 1} "
                           "minutes.", "error")
            return _back()
        outcome = await run_in_threadpool(services.send_test_email)
        address = config.get_settings().mailbox_address()
        if not outcome.ok:
            flash(request, f"The test email wasn't sent: {outcome.text}", "error")
            return _back()
        # Sending works (a refused login is lifted); reading the inbox is the other half (replies, opt-outs and
        # bounces).
        await run_in_threadpool(partial(repo.retry_mailbox, auth_only=True))
        _inbox_flash(request, await run_in_threadpool(services.check_inbox),
                     f"Test email sent to {address}: check that it arrived (also in the spam folder). ")
    elif action == "check":
        if wait := limits(request).inbox_check.allow("mailbox"):
            flash(request, "You checked the inbox many times in the last 10 minutes. Try again in a few minutes.",
                  "error")
            return _back()
        result = await run_in_threadpool(services.check_inbox)
        if result["status"] == "ok":  # the reading login works again: the automatic checks start again
            await run_in_threadpool(partial(repo.retry_mailbox, auth_only=True))
        _inbox_flash(request, result)
    elif action == "retry":
        await run_in_threadpool(repo.retry_mailbox)
        flash(request, "Mailbox pause lifted. OpenBerry tries again within a minute, if email sending is on.")
    elif action == "disconnect":
        await run_in_threadpool(_disconnect, request)
    elif action == "unsuppress":
        raw = _text(form, "suppression_id")
        if raw.isdecimal():
            await run_in_threadpool(repo.remove_suppression, int(raw))
            flash(request, "Removed. OpenBerry may email that address again.", "info")
    else:
        flash(request, "Nothing was changed.", "info")
    return _back()


def _disconnect(request: Request) -> None:
    if not _save(request, {name: None for name in config.MAILBOX_SETTINGS if _editable(name)}):
        return
    for company in repo.list_companies():
        if company.outreach.email_sending:
            repo.update_company(company.id, {"outreach": {"email_sending": False}})
    outside = [name for name in config.MAILBOX_SETTINGS if config.setting_source(name)[0] and not _editable(name)]
    if outside:  # the flash says only what was removed
        flash(request, f"Email sending is off for every company. Some mailbox settings are set outside OpenBerry "
                       f"({', '.join(outside)}): remove them there to disconnect the mailbox.", "warning")
        return
    flash(request, "Mailbox disconnected and its password removed. Email sending is off for every company.", "info")


# --------------------------------------------------------------------------------------
# The Outreach page's Email sending card
# --------------------------------------------------------------------------------------

EMAIL_STATE_LABELS = {
    "off": "Off", "on": "On", "not_connected": "Mailbox not connected", "mailbox_paused": "Mailbox paused",
    "inbox_unchecked": "Inbox not checked", "footer": "Footer needed", "company_paused": "Company paused",
    "paused": "Paused", "daily_limit": "Daily limit reached", "mailbox_limit": "Daily limit reached",
    "outside_hours": "Outside sending hours",
}
# The dashboard's line: "Email sending is <phrase>: 4 of 30 emails sent in the last 24 hours".
EMAIL_STATE_PHRASES = {
    "on": "on", "outside_hours": "on", "not_connected": "on, but no mailbox is connected",
    "mailbox_paused": "on, but your mailbox is paused", "inbox_unchecked": "on, but your inbox wasn't checked",
    "inbox_problem": "on, but OpenBerry can't read your inbox", "footer": "on, but the footer needs fixing",
    "company_paused": "on, but the company is paused", "paused": "paused",
    "daily_limit": "on, but the daily limit is reached",
    "mailbox_limit": "on, but your mailbox's daily limit is reached",
}
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-]\d{2}:\d{2}|Z)")


def for_you(text: str) -> str:
    """A reason shared with Claude and the API (times in ISO, in UTC), for the dashboard: on this computer's clock."""
    return _ISO_TIME.sub(lambda m: local_dt(to_datetime(m.group(0))), text or "")
EMAIL_LIMIT_RANGE = forms.EMAIL_LIMIT_RANGE
EMAIL_GAP_RANGE = forms.EMAIL_GAP_RANGE
EMAIL_QUEUE_SHOWN = 10
EMAIL_SKIPPED_SHOWN = 20
# The queue's blocked_reason values that only delay emails that are otherwise ready to go: they stay "queued".
DELAYED = ("", "outside_hours")


def email_status(company: Company, messages: list[Message] | None = None) -> dict[str, Any]:
    """Email sending for the dashboard: the queue (repo.email_queue, every rule applied), the card's state and, for
    `messages`, what happens to each email (`info`: queued, sending, held or waiting with the reason, auto (approved
    by auto-approve, which isn't allowed to send), unknown, failed, bounced). Reading it changes nothing."""
    config.refresh_saved_settings()
    settings = config.get_settings()
    queue = repo.email_queue(company.id, limit=repo.EMAIL_QUEUE_MAX)
    reason = queue["blocked_reason"]
    state = "off" if reason == "disabled" else (reason or "on")
    attention = repo.email_attention(company.id)
    eligible = {item["message_id"] for item in queue["items"]}
    skipped = [{**s, "reason": for_you(s["reason"])} for s in queue["skipped"]]
    held = {s["message_id"]: s["reason"] for s in skipped}
    blocked_message = for_you(repo.email_blocked_message(queue, you=True))
    # Failed because the address doesn't exist (it is on the never-email list): Try again can't work, so the
    # email offers "Fix the address" instead.
    failed = [m for m in [*(messages or []), *attention["messages"]] if m.email_state == "failed"]
    never = repo.suppressed_addresses([m.email_to or "" for m in failed])
    dead_address = {m.id for m in failed
                    if (never.get(repo.normalize_email(m.email_to or "")) or {}).get("reason") == "bounce"}
    info: dict[int, dict[str, Any]] = {}
    for msg in messages or []:
        if msg.channel != "email" or msg.direction != "outbound":
            continue
        if msg.email_state in repo.EMAIL_IN_FLIGHT:
            info[msg.id] = {"state": "sending"}
        elif msg.email_state in ("unknown", "failed", "bounced"):
            info[msg.id] = {"state": msg.email_state, "reason": msg.send_error, "dead": msg.id in dead_address}
        elif msg.status != "approved" or not queue["enabled"]:
            continue
        elif msg.id in eligible:
            info[msg.id] = {"state": "queued"} if reason in DELAYED else {"state": "waiting", "reason": blocked_message}
        elif msg.approved_via == APPROVED_VIA_AUTO and not company.outreach.email_send_auto_approved:
            info[msg.id] = {"state": "auto", "reason": held.get(msg.id, "")}
        elif msg.id in held:
            info[msg.id] = {"state": "held", "reason": held[msg.id]}
    preview = next((m for m in messages or [] if m.channel == "email" and m.direction == "outbound"
                    and m.status in ("approved", "draft")), None)
    if preview is None:
        preview = next((m for m in repo.list_messages(company.id, direction="outbound", limit=50)
                        if m.channel == "email" and m.status in ("approved", "draft")), None)
    inbox_problem = state == "inbox_unchecked" and bool(queue["mailbox"]["inbox_error"])
    return {
        **queue, "items": queue["items"][:EMAIL_QUEUE_SHOWN], "skipped": skipped, "state": state,
        "label": "Inbox problem" if inbox_problem else EMAIL_STATE_LABELS.get(state, state),
        "phrase": EMAIL_STATE_PHRASES.get("inbox_problem" if inbox_problem else state, "on"),
        "info": info, "message": blocked_message, "dead_address": dead_address,
        "queued_ids": eligible if reason in DELAYED else set(),
        "from_email": settings.smtp_from_email or settings.smtp_username, "from_name": settings.smtp_from_name,
        "provider": settings.mail_provider, "hostinger": settings.mail_provider == "hostinger",
        "next_send_at": to_datetime(queue["next_send_at"]), "limit_frees_at": to_datetime(queue["limit_frees_at"]),
        # The email loop runs every minute: an email that may go now goes within a minute.
        "next_soon": bool(queue["next_send_at"]) and to_datetime(queue["next_send_at"]) <= repo.utcnow() + timedelta(
            minutes=1),
        "paused_until": to_datetime(queue["paused_until"]),
        "mailbox_paused_until": to_datetime(queue["mailbox"]["paused_until"]),
        "inbox_checked_at": to_datetime(queue["mailbox"]["inbox_checked_at"]),
        "attention": attention["messages"], "events": attention["events"],
        "footer_text": company.outreach.email_footer or default_footer(company),
        # Added under every email OpenBerry sends: shown under each email draft while email sending is on.
        "footer_added": company.outreach.email_footer if queue["enabled"] else "",
        "preview": preview, "weekdays": WEEKDAYS,
        "hours_text": sending_hours_text(queue["sending_days"], *queue["sending_hours"]),
        "approved_waiting": queue["eligible_total"] + sum(1 for s in queue["skipped"]
                                                          if s["state"] not in ("unknown", "failed", "bounced")),
        "skipped_shown": EMAIL_SKIPPED_SHOWN,
        "sending_stopped": services.sending_stopped(),
    }


def sending_hours_text(days: list[int], start: int, end: int) -> str:
    """The sending hours in words: "Mon to Fri, 09:00 to 18:00", "every day, ...", "Mon, Wed, ..."."""
    days = sorted(set(days))
    if len(days) == len(WEEKDAYS):
        when = "every day"
    elif len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        when = f"{WEEKDAYS[days[0]]} to {WEEKDAYS[days[-1]]}"
    else:
        when = ", ".join(WEEKDAYS[d] for d in days) or "no day"
    return f"{when}, {start:02d}:00 to {end:02d}:00"


def email_lead_ids(email: dict[str, Any]) -> list[int]:
    """The leads the Email sending card names (its skipped list, the emails needing attention, bounces, opt-outs)."""
    return [*(s["lead_id"] for s in email["skipped"][:EMAIL_SKIPPED_SHOWN]),
            *(m.lead_id for m in email["attention"]), *(e["lead_id"] for e in email["events"] if e["lead_id"])]


def _whole(raw: Any, low: int, high: int) -> int | None:
    text = raw.strip() if isinstance(raw, str) else ""
    return int(text) if text.isdecimal() and low <= int(text) <= high else None


def _email_patch(form: FormData) -> tuple[dict[str, Any], list[str]]:
    """The card's settings as an outreach patch, and what is wrong with them."""
    problems = []
    limit = _whole(form.get("email_daily_limit"), *EMAIL_LIMIT_RANGE)
    gap = _whole(form.get("email_gap_minutes"), *EMAIL_GAP_RANGE)
    start = _whole(form.get("email_start_hour"), 0, 23)
    end = _whole(form.get("email_end_hour"), 1, 24)
    days = sorted({int(d) for d in form.getlist("email_days") if isinstance(d, str) and d.isdecimal() and int(d) < 7})
    footer = (form.get("email_footer") if isinstance(form.get("email_footer"), str) else "") or ""
    footer = footer.replace("\r\n", "\n").strip()
    if limit is None:
        problems.append(f"The daily limit must be a whole number from {EMAIL_LIMIT_RANGE[0]} to "
                        f"{EMAIL_LIMIT_RANGE[1]}.")
    if gap is None:
        problems.append(f"The minutes between two emails must be a whole number from {EMAIL_GAP_RANGE[0]} to "
                        f"{EMAIL_GAP_RANGE[1]}.")
    if start is None or end is None or end <= start:
        problems.append("The sending hours must end after they start.")
    if not days:
        problems.append("Choose at least one day to send on.")
    if len(footer) > 600:
        problems.append("Keep the footer under 600 characters.")
    patch = {"email_daily_limit": limit, "email_gap_minutes": gap, "email_start_hour": start, "email_end_hour": end,
             "email_days": days, "email_footer": footer,
             "email_send_auto_approved": forms.checked(form, "email_send_auto_approved")}
    return patch, problems


@router.post("/c/{company_id}/outreach/email")
def email_settings(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    """action = on | save | off. Turning it off always works; turning it on needs a connected mailbox, a good inbox
    check, a footer that says who you are and how to opt out, and the warnings acknowledged."""
    company = repo.get_company(company_id)
    back = f"/c/{company_id}/outreach#email"
    action = _text(form, "action")
    if action == "off":
        repo.update_company(company_id, {"outreach": {"email_sending": False}})
        flash(request, "Email sending is off. Approved emails wait for you to send them yourself.", "info")
        return RedirectResponse(back, status_code=303)
    patch, problems = _email_patch(form)
    if problems:
        for problem in problems:
            flash(request, problem, "error")
        flash(request, "Nothing was changed.", "error")
        return RedirectResponse(back, status_code=303)
    turn_on = action == "on" and not company.outreach.email_sending
    if company.outreach.email_sending and (footer_issues := footer_problems(company, patch["email_footer"])):
        # While it is on, every email gets this footer: never save one that says less than the law asks.
        for problem in footer_issues:
            flash(request, f"Footer: {problem[:1].upper()}{problem[1:]}.", "error")
        flash(request, "Nothing was changed.", "error")
        return RedirectResponse(back, status_code=303)
    if turn_on:
        address = config.get_settings().mailbox_address()
        mailbox = repo.mailbox_state(address) if address else None
        footer_issues = footer_problems(company, patch["email_footer"])
        blockers = []
        if mailbox is None:
            blockers.append("Connect your mailbox first, on the Mailbox page.")
        elif not mailbox["inbox_ok"]:
            blockers.append("Check your inbox on the Mailbox page first: OpenBerry must be able to read replies and "
                            "opt-outs before it sends anything.")
        blockers += [f"Footer: {p[:1].upper()}{p[1:]}." for p in footer_issues]
        if not forms.checked(form, "acknowledge"):
            blockers.append("Tick the box to say you've read the warnings.")
        if blockers:
            for blocker in blockers:
                flash(request, blocker, "error")
            repo.update_company(company_id, {"outreach": patch})  # keep what they typed
            flash(request, "Settings saved, but email sending is still off.", "warning")
            return RedirectResponse(back, status_code=303)
        patch["email_sending"] = True
    was_auto = company.outreach.email_send_auto_approved
    company = repo.update_company(company_id, {"outreach": patch})
    out = company.outreach
    if turn_on:
        flash(request, f"Email sending is on: OpenBerry emails the emails you approve from your mailbox, at most "
                       f"{out.email_daily_limit} a day, at least {out.email_gap_minutes} minutes apart, while "
                       "OpenBerry is open.")
    else:
        flash(request, "Email settings saved.")
    if out.email_send_auto_approved and not was_auto:
        flash(request, "Emails that auto-approve approves are sent too: nobody reads them before they go out."
              + ("" if out.auto_approve else " Auto-approve is off now, so this changes nothing until you turn it "
                                               "on."), "warning")
    return RedirectResponse(back, status_code=303)


@router.post("/c/{company_id}/outreach/email/resume")
def email_resume(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    """Lift the company's email pause (set after bounces or policy rejections)."""
    company = repo.get_company(company_id)
    repo.resume_email_sending(company_id)
    flash(request, "Email sending resumed." if company.outreach.email_sending
          else "Pause lifted. Email sending is still off.")
    return RedirectResponse(safe_next(form.get("next"), f"/c/{company_id}/outreach#email"), status_code=303)


@router.post("/c/{company_id}/outreach/email/send-now")
async def email_send_now(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    """Wake the email loop, or, with the scheduler off (OPENBERRY_SCHEDULER=false), send one email in this request.
    Every rule still applies, including the minutes between two emails."""
    company = await run_in_threadpool(repo.get_company, company_id)
    back = f"/c/{company_id}/outreach#email"
    wake = getattr(request.app.state, "email_wake", None)
    if wake is not None:
        wake.set()
        flash(request, f"Sending now, if nothing blocks it. The rest go out one at a time, at least "
                       f"{company.outreach.email_gap_minutes} minutes apart, while OpenBerry is open.", "info")
        return RedirectResponse(back, status_code=303)
    if await run_in_threadpool(services.inbox_due):
        await run_in_threadpool(services.check_inbox)
    result = await run_in_threadpool(services.send_next_email)
    if result["status"] == "sent":
        flash(request, "1 email sent. Turn the scheduler on to send the rest automatically.")
    elif result["status"] == "idle":
        status = await run_in_threadpool(repo.email_sending_status, company_id)
        why = for_you(repo.email_blocked_message(status, you=True))
        if not why and status.get("next_send_at"):
            why = f"the next email can go out at {local_dt(to_datetime(status['next_send_at']))}"
        flash(request, f"Nothing was sent{': ' + why if why else ': no approved email is ready'}.", "info")
    else:
        flash(request, f"Not sent: {result.get('text') or result['status']}", "error")
    return RedirectResponse(back, status_code=303)


EMAIL_ACTIONS = {
    "mark_sent": "Marked as sent from your mailbox.",
    "send_again": "It goes back in the queue and is sent again.",
    "retry": "It goes back in the queue and is tried again.",
}


@router.post("/c/{company_id}/messages/{message_id}/email")
def email_message_action(request: Request, company_id: int, message_id: int,
                         form: FormData = Depends(checked_form)) -> Response:
    """An email that may have been sent (Mark sent, Send again) or failed (Try again)."""
    message = repo.get_message(message_id)
    if message.company_id != company_id:
        raise repo.NotFound(f"message {message_id} not found")
    back = safe_next(form.get("next"), f"/c/{company_id}/outreach#email")
    action = _text(form, "action")
    if action not in EMAIL_ACTIONS:
        flash(request, "Unknown action.", "error")
        return RedirectResponse(back, status_code=303)
    try:
        repo.resolve_email(message_id, action)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(back, status_code=303)
    # Say what really happens to it now: back in the queue, or held back (a bounced address, a reply...).
    held = next((s["reason"] for s in repo.email_queue(company_id, limit=0)["skipped"]
                 if s["message_id"] == message_id), "") if action != "mark_sent" else ""
    if held:
        flash(request, f"It isn't emailed now: {for_you(held)}.", "warning")
    else:
        flash(request, EMAIL_ACTIONS[action])
    return RedirectResponse(back, status_code=303)


@router.post("/c/{company_id}/leads/{lead_id}/unsubscribe")
def lead_unsubscribe(request: Request, company_id: int, lead_id: int,
                     form: FormData = Depends(checked_form)) -> Response:
    """The lead asked not to be contacted: never email their address again (any company), disqualify the lead."""
    lead = repo.get_lead(lead_id)
    if lead.company_id != company_id:
        raise repo.NotFound(f"lead {lead_id} not found in company {company_id}")
    repo.record_opt_out(lead_id, "marked in the dashboard", "You marked it")
    flash(request, f"{lead.display_name} is unsubscribed: OpenBerry never emails {lead.email or 'them'} again, and the "
                   "lead is disqualified." if lead.email else f"{lead.display_name} is disqualified.", "info")
    return RedirectResponse(f"/c/{company_id}/leads/{lead_id}", status_code=303)

