"""Lead pages: list with filters, CSV import/export, add lead, lead detail, and outreach messages."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.responses import Response

from .. import leads_csv, outreach, repo
from ..config import get_settings
from ..models import AGENT_CHANNELS, APPROVED_VIA_AUTO, LEAD_STATUSES, MESSAGE_CHANNELS, TIERS, Company, Lead, Message
from ..repo import AGENT_QUEUE_MAX
from . import forms
from .auth import checked_form, require_login
from .pages import hours_text, redirect
from .session import flash, remember_company, safe_next
from .ui import LEAD_SOURCES, choice, int_param, page_info, render

router = APIRouter(dependencies=[Depends(require_login)], include_in_schema=False)

LEAD_SORTS = {"score": "Score", "recent": "Recently added", "signal": "Latest signal", "name": "Name"}
MAX_CSV_BYTES = 5 * 1024 * 1024
MESSAGE_ACTIONS = {"save": None, "approve": "approved", "sent": "sent", "skip": "skipped", "draft": "draft"}
# Auto-approve: hold a waiting draft (it is never approved automatically), or let a held one auto-approve again.
HOLD_ACTIONS = {"hold": True, "release": False}


def _lead_filters(tier: str, status: str, kind: str, source: str, q: str, sort: str) -> dict[str, Any]:
    return {
        "tier": choice(tier, TIERS), "status": choice(status, LEAD_STATUSES),
        "kind": choice(kind, ("person", "account")), "source": choice(source, LEAD_SOURCES),
        "search": q.strip()[:200] or None, "sort": choice(sort, LEAD_SORTS) or "score",
    }


def _company_lead(company_id: int, lead_id: int) -> tuple[Company, Lead]:
    company = repo.get_company(company_id)
    lead = repo.get_lead(lead_id)
    if lead.company_id != company_id:
        raise repo.NotFound(f"lead {lead_id} not found in company {company_id}")
    return company, lead


def _form_text(form: FormData, name: str, default: str = "") -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else default


# --------------------------------------------------------------------------------------
# Leads list, export, import, add
# --------------------------------------------------------------------------------------

@router.get("/c/{company_id}/leads")
def leads_page(request: Request, company_id: int, tier: str = "", status: str = "", kind: str = "",
               source: str = "", q: str = "", sort: str = "score", page: str = "1") -> Response:
    company = repo.get_company(company_id)
    remember_company(request, company_id)
    filters = _lead_filters(tier, status, kind, source, q, sort)
    _, total = repo.list_leads(company_id, **filters, limit=1)
    pager = page_info(int_param(page), total)
    items, total = repo.list_leads(company_id, **filters, limit=pager["size"], offset=pager["offset"])
    any_leads = total > 0 or bool(repo.list_leads(company_id, limit=1)[1])
    return render(request, "leads.html", {
        "company": company, "active": "leads", "title": "Leads", "items": items, "pager": pager,
        "filters": {k: v or "" for k, v in filters.items()}, "sorts": LEAD_SORTS, "any_leads": any_leads,
        "filtered": any(filters[k] for k in ("tier", "status", "kind", "source", "search")),
    })


@router.get("/c/{company_id}/leads.csv")
def leads_export(company_id: int, tier: str = "", status: str = "", kind: str = "", source: str = "",
                 q: str = "", sort: str = "score") -> Response:
    company = repo.get_company(company_id)
    items, _ = repo.list_leads(company_id, **_lead_filters(tier, status, kind, source, q, sort), limit=5000)
    slug = "".join(ch if ch.isalnum() else "-" for ch in company.name.lower()).strip("-")[:40] or "company"
    filename = f"openberry-leads-{slug}-{date.today().isoformat()}.csv"
    return Response(leads_csv.export_leads_csv(items), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _nothing_imported_reason(stats: dict[str, Any]) -> str:
    if stats["errors"] and not stats["skipped"]:
        return "every row failed (the first problems are listed below)."
    if stats["skipped"]:
        rows = f"{stats['skipped']} row{'s' * (stats['skipped'] != 1)}"
        return f"none of its {rows} had a name or company. Are the column headers the first line of the file?"
    return "it has no rows under the header line."


@router.post("/c/{company_id}/leads/import")
def leads_import(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    repo.get_company(company_id)
    upload = form.get("file")
    back = f"/c/{company_id}/leads"
    if not isinstance(upload, UploadFile) or not upload.filename:
        flash(request, "Choose a CSV file to import.", "error")
        return redirect(back)
    raw = upload.file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        flash(request, "That file is larger than 5 MB. Split it and import the parts.", "error")
        return redirect(back)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        stats = leads_csv.import_leads_csv(company_id, text)
    except ValueError as exc:
        flash(request, f"Import failed: {exc}", "error")
        return redirect(back)
    if stats["created"] + stats["merged"]:
        flash(request, f"Imported {upload.filename}: {stats['created']} new, {stats['merged']} merged into existing "
                       f"leads, {stats['skipped']} skipped.")
    else:
        flash(request, f"Nothing was imported from {upload.filename}: {_nothing_imported_reason(stats)}", "warning")
    for err in stats["errors"][:5]:
        flash(request, err, "warning")
    return redirect(back)


@router.get("/c/{company_id}/leads/new")
def lead_new_page(request: Request, company_id: int, lead_company: str = "", company_domain: str = "") -> Response:
    """Add-lead form; ?lead_company=&company_domain= pre-fill it (e.g. a person at an account lead)."""
    company = repo.get_company(company_id)
    return render(request, "lead_new.html", {
        "company": company, "active": "leads", "title": "Add lead",
        "values": {"signal_type": "custom", "signal_strength": "50", "signal_date": date.today().isoformat(),
                   "lead_company": lead_company.strip()[:200], "company_domain": company_domain.strip()[:200]},
    })


@router.post("/c/{company_id}/leads")
def lead_create(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    company = repo.get_company(company_id)
    data, values, errors = forms.lead_from_form(form)
    if data is not None:
        try:
            lead, created = repo.upsert_lead(company_id, data)
        except ValueError as exc:
            errors = {"full_name": str(exc)}
        else:
            flash(request, "Lead added." if created else "This lead already existed; new details were merged in.")
            return redirect(f"/c/{company_id}/leads/{lead.id}")
    return render(request, "lead_new.html", {
        "company": company, "active": "leads", "title": "Add lead", "values": values, "errors": errors,
    }, status_code=422)


# --------------------------------------------------------------------------------------
# Lead detail
# --------------------------------------------------------------------------------------

@router.get("/c/{company_id}/leads/{lead_id}")
def lead_page(request: Request, company_id: int, lead_id: int) -> Response:
    company, lead = _company_lead(company_id, lead_id)
    remember_company(request, company_id)
    repo.auto_approve_due(company_id)  # the drafts below are current, also with the scheduler off
    signals, signal_total = repo.list_signals(company_id, lead_id=lead_id, include_account=True, limit=100)
    messages = sorted(repo.list_messages(company_id, lead_id=lead_id, limit=500), key=lambda m: (m.created_at, m.id))
    contacts = repo.contacts_at_account(lead)
    channel, step = outreach.next_touch(company, lead, messages)
    return render(request, "lead.html", {
        "company": company, "lead": lead, "active": "leads", "title": lead.display_name,
        "signals": signals, "signal_total": signal_total, "messages": messages, "contacts": contacts,
        "auto_states": repo.auto_approve_states(company, messages) if company.outreach.auto_approve else {},
        "default_channel": channel, "next_step": step,
        "claude_prompt": f"Use openberry: get the outreach context for lead {lead.id} and write a "
                         f"{channel} message{f' for step {step}' if step > 1 else ''}, then save it.",
        # Copied prompts become the user's own words, so they name records by id only (never lead text).
        "decision_maker_prompt": f"Use openberry: find the decision-maker for account lead {lead.id} of company "
                                 f"{company_id} (read it with get_lead) and add them as a lead.",
        "values": {"signal_type": "custom", "signal_strength": "50", "signal_date": date.today().isoformat()},
    })


@router.post("/c/{company_id}/leads/{lead_id}/update")
def lead_update(request: Request, company_id: int, lead_id: int, form: FormData = Depends(checked_form)) -> Response:
    _company_lead(company_id, lead_id)
    fields: dict[str, Any] = {}
    if "status" in form:
        fields["status"] = _form_text(form, "status")
    for name in ("notes", "tags"):
        if name in form:
            fields[name] = _form_text(form, name)
    try:
        repo.update_lead(lead_id, fields)
        flash(request, "Lead updated.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return redirect(f"/c/{company_id}/leads/{lead_id}")


@router.post("/c/{company_id}/leads/{lead_id}/profile")
def lead_profile(request: Request, company_id: int, lead_id: int, form: FormData = Depends(checked_form)) -> Response:
    _, lead = _company_lead(company_id, lead_id)
    fields = forms.profile_from_form(form)
    if not fields.get("full_name", lead.full_name) and not fields.get("lead_company", lead.lead_company):
        flash(request, "A lead needs a name or a company.", "error")
    else:
        approved = len(repo.list_messages(company_id, status="approved", lead_id=lead_id))
        repo.update_lead(lead_id, fields)
        if len(repo.list_messages(company_id, status="approved", lead_id=lead_id)) < approved:
            flash(request, "Profile saved and lead rescored. The LinkedIn profile changed, so approved LinkedIn "
                           "messages to this lead are drafts again: approve them for the new profile.", "info")
        else:
            flash(request, "Profile saved and lead rescored.")
    return redirect(f"/c/{company_id}/leads/{lead_id}")


@router.post("/c/{company_id}/leads/{lead_id}/signals")
def lead_add_signal(request: Request, company_id: int, lead_id: int,
                    form: FormData = Depends(checked_form)) -> Response:
    _company_lead(company_id, lead_id)
    signal, errors = forms.signal_from_form(form)
    if signal is None:
        flash(request, next(iter(errors.values()), "Give the signal a title or a link."), "error")
    else:
        _, created = repo.add_signal(company_id, signal, lead_id=lead_id)
        flash(request, "Signal added and lead rescored." if created else "That signal was already recorded.")
    return redirect(f"/c/{company_id}/leads/{lead_id}#signals")


@router.post("/c/{company_id}/leads/{lead_id}/delete")
def lead_delete(request: Request, company_id: int, lead_id: int, form: FormData = Depends(checked_form)) -> Response:
    _, lead = _company_lead(company_id, lead_id)
    repo.delete_lead(lead_id)
    flash(request, f"Deleted {lead.display_name}.", "info")
    return redirect(f"/c/{company_id}/leads")


@router.post("/c/{company_id}/leads/{lead_id}/draft")
async def lead_draft(request: Request, company_id: int, lead_id: int,
                     form: FormData = Depends(checked_form)) -> Response:
    company, lead = await run_in_threadpool(_company_lead, company_id, lead_id)
    back = f"/c/{company_id}/leads/{lead_id}#outreach"
    previous = await run_in_threadpool(lambda: repo.list_messages(company_id, lead_id=lead_id, limit=500))
    # The "Draft follow-up" buttons post only the step: continue the sequence (LinkedIn DM after a connection note).
    next_channel, next_step = outreach.next_touch(company, lead, previous)
    channel = choice(_form_text(form, "channel"), MESSAGE_CHANNELS) or next_channel
    step = int_param(_form_text(form, "step"), default=next_step, high=10)
    engine = "ollama" if _form_text(form, "engine") == "ollama" else "template"
    signals, _ = await run_in_threadpool(lambda: repo.list_signals(company_id, lead_id=lead_id,
                                                                   include_account=True, limit=20))
    if engine == "ollama":
        context = outreach.outreach_context(company, lead, signals, previous, channel, step)
        try:
            subject, body = await outreach.draft_with_ollama(get_settings(), context)
        except Exception as exc:  # external service: a bad URL or a non-JSON reply must not cost the page
            flash(request, f"Local AI draft failed: {exc}", "error")
            return redirect(back)
    else:
        subject, body = outreach.draft_template(company, lead, signals, channel, step)
    msg = await run_in_threadpool(lambda: repo.create_message(lead_id, body, channel=channel, subject=subject,
                                                              step=step, generated_by=engine))
    flash(request, "Draft created. Review and edit it before you send it.")
    return redirect(f"/c/{company_id}/leads/{lead_id}#msg-{msg.id}")


@router.post("/c/{company_id}/leads/{lead_id}/reply")
def lead_reply(request: Request, company_id: int, lead_id: int, form: FormData = Depends(checked_form)) -> Response:
    _company_lead(company_id, lead_id)
    body = _form_text(form, "body")
    channel = choice(_form_text(form, "channel"), MESSAGE_CHANNELS) or "linkedin_dm"
    if not body:
        flash(request, "Paste the reply text first.", "error")
        return redirect(f"/c/{company_id}/leads/{lead_id}#outreach")
    msg = repo.log_reply(lead_id, body, channel=channel)
    flash(request, "Reply logged. The lead moved to 'replied' and pending drafts were skipped.")
    return redirect(f"/c/{company_id}/leads/{lead_id}#msg-{msg.id}")


# --------------------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------------------

def _approved_note(company_id: int, message: Message) -> str:
    """What happens to a message just approved: the user sends it, or their AI agent may."""
    if message.channel not in AGENT_CHANNELS or not repo.get_company(company_id).outreach.agent_sending:
        return "Approved. Copy it and send it from LinkedIn or your inbox."
    queue = repo.send_queue(company_id, limit=AGENT_QUEUE_MAX)
    why_not = next((s["reason"] for s in queue.get("skipped") or [] if s["message_id"] == message.id), "")
    if why_not:
        return (f"Approved. It isn't in your AI agent's queue right now ({why_not}); you can copy it and send it "
                "yourself.")
    return "Approved. Your AI agent will send it exactly as it is, or copy it and send it yourself."


def _bulk_selection(form: FormData) -> list[tuple[int, str]]:
    """The ticked drafts as (message id, message_version) pairs; malformed values are ignored."""
    selected: list[tuple[int, str]] = []
    for value in form.getlist("message"):
        raw_id, _, version = value.partition(":") if isinstance(value, str) else ("", "", "")
        if raw_id.isdecimal() and version and (int(raw_id), version) not in selected:
            selected.append((int(raw_id), version))
    return selected


def _bulk_problems(problems: dict[str, int]) -> str:
    parts = [f"{count} {reason}" for reason, count in problems.items()]
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]


@router.post("/c/{company_id}/outreach/bulk")
def bulk_message_action(request: Request, company_id: int, form: FormData = Depends(checked_form)) -> Response:
    """Approve or skip the drafts ticked on the Outreach page's Drafts tab."""
    company = repo.get_company(company_id)
    back = safe_next(form.get("next"), f"/c/{company_id}/outreach?tab=drafts")
    action = _form_text(form, "action")
    if action not in repo.BULK_ACTIONS:
        flash(request, "Unknown action.", "error")
        return redirect(back)
    selected = _bulk_selection(form)
    if not selected:
        flash(request, "Tick the drafts you want first, then choose Approve or Skip.", "error")
        return redirect(back)
    if len(selected) > repo.BULK_MESSAGES_MAX:
        flash(request, f"Select at most {repo.BULK_MESSAGES_MAX} drafts at a time. Nothing was changed.", "error")
        return redirect(back)
    result = repo.bulk_update_drafts(company_id, selected, action)
    done, problems = result["done"], result["problems"]
    verb = "Approved" if action == "approve" else "Skipped"
    if done:
        text = f"{verb} {len(done)} draft{'' if len(done) == 1 else 's'}."
        if action == "approve":
            text += (" Your AI agent can send the LinkedIn ones." if company.outreach.agent_sending
                     else f" Send {'it' if len(done) == 1 else 'them'} from the Approved tab.")
        if problems:
            text += f" {sum(problems.values())} not {verb.lower()}: {_bulk_problems(problems)}."
    else:
        text = f"Nothing was {verb.lower()}: {_bulk_problems(problems)}."
    if repo.BULK_CHANGED in problems:
        text += " Review the edited ones, then try again."
    flash(request, text, "success" if done and not problems else "warning" if done else "error")
    return redirect(back)


@router.post("/c/{company_id}/messages/{message_id}")
def message_action(request: Request, company_id: int, message_id: int,
                   form: FormData = Depends(checked_form)) -> Response:
    message = repo.get_message(message_id)
    if message.company_id != company_id:
        raise repo.NotFound(f"message {message_id} not found")
    back = safe_next(form.get("next"), f"/c/{company_id}/leads/{message.lead_id}#outreach")
    action = _form_text(form, "action", "save")
    if action == "delete":
        repo.delete_message(message_id)
        flash(request, "Message deleted.", "info")
        return redirect(back.split("#", 1)[0])  # its #msg anchor is gone: the form's data-keep-scroll keeps the place
    if action not in MESSAGE_ACTIONS and action not in HOLD_ACTIONS:
        flash(request, "Unknown action.", "error")
        return redirect(back)
    edits: dict[str, Any] = {}
    if "body" in form and message.direction == "outbound":
        edits["body"] = _form_text(form, "body")
    if "subject" in form and message.direction == "outbound":
        edits["subject"] = _form_text(form, "subject")
    if action in HOLD_ACTIONS:
        return _hold_action(request, company_id, message, HOLD_ACTIONS[action], back, edits)
    try:
        updated = repo.update_message(message_id, status=MESSAGE_ACTIONS[action], **edits)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(back)
    if message.status == "approved" and updated.status == "draft" and action == "save":
        # repo.update_message: an approval covers the exact text, so changed text waits for approval again.
        company = repo.get_company(company_id)
        if _auto_state(company, updated).get("state") == "waiting":
            flash(request, f"Saved as a draft: you changed the approved text. Auto-approve approves it in "
                           f"{hours_text(company.outreach.auto_approve_hours)} unless you approve, hold or skip it "
                           "first.", "info")
        else:
            flash(request, "Saved as a draft: you changed the approved text. Approve it again when it's ready.",
                  "info")
        return redirect(back)
    draft_note = "Moved back to drafts."
    if updated.auto_hold and message.status == "approved" and repo.get_company(company_id).outreach.auto_approve:
        draft_note = "Moved back to drafts and put on hold: it won't be approved automatically. Approve it yourself."
    flash(request, {
        "save": "Message saved.", "approve": _approved_note(company_id, message),
        "sent": "Marked as sent. The follow-up timer has started.", "skip": "Message skipped.",
        "draft": draft_note,
    }[action])
    return redirect(back)


def _auto_state(company: Company, message: Message) -> dict[str, Any]:
    """What auto-approve does next with a draft (repo.auto_approve_states), or {} while it is off."""
    if not company.outreach.auto_approve:
        return {}
    return repo.auto_approve_states(company, [message]).get(message.id, {})


def _hold_action(request: Request, company_id: int, message: Message, hold: bool, back: str,
                 edits: dict[str, str]) -> Response:
    """Hold a draft, so auto-approve never approves it, or let a held draft auto-approve again.

    On the lead page the buttons belong to the draft's edit form: text typed there is saved first, not lost.
    Hold on a message auto-approve approved since the page was opened takes that approval back (set_auto_hold).
    """
    changed = {key: value for key, value in edits.items()
               if (repo.message_text(value) if key == "body" else value) != getattr(message, key)}
    if changed and message.status in ("draft", "approved"):  # never the text of a message sent in the meantime
        try:
            repo.update_message(message.id, **changed)
        except ValueError as exc:  # e.g. an empty message
            flash(request, str(exc), "error")
            return redirect(back)
    try:
        updated = repo.set_auto_hold(message.id, hold)
    except ValueError:  # not a draft (any more): approved by hand, sent or skipped in the meantime
        flash(request, "Only drafts can be put on hold or let auto-approve, and this message isn't a draft.", "error")
        return redirect(back)
    company = repo.get_company(company_id)
    state = _auto_state(company, updated)
    if hold and message.status == "approved" and message.approved_via == APPROVED_VIA_AUTO:
        flash(request, "It had just been approved automatically. It's back in drafts and on hold: approve it "
                       "yourself when it's ready.", "info")
    elif hold:
        flash(request, "On hold: this draft won't be approved automatically. Approve it yourself when it's ready.",
              "info")
    elif state.get("state") == "waiting":
        flash(request, f"Hold lifted: this draft is approved automatically in "
                       f"{hours_text(company.outreach.auto_approve_hours)} unless you approve, hold or skip it first. "
                       "Editing it starts the window again.")
    elif state.get("state") == "blocked":
        flash(request, f"Hold lifted, but it isn't approved automatically for now: {state['reason']}.")
    else:
        flash(request, "Hold lifted.")
    return redirect(back)
