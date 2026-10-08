"""Lead pages: list with filters, CSV import/export, add lead, lead detail, and outreach messages."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.responses import Response

from .. import leads_csv, outreach, repo, services
from ..config import get_settings
from ..models import LEAD_STATUSES, MESSAGE_CHANNELS, TIERS, Company, Lead
from . import forms
from .auth import checked_form, require_login
from .pages import redirect
from .session import flash, remember_company, safe_next
from .ui import LEAD_SOURCES, choice, int_param, page_info, render

router = APIRouter(dependencies=[Depends(require_login)], include_in_schema=False)

LEAD_SORTS = {"score": "Score", "recent": "Recently added", "signal": "Latest signal", "name": "Name"}
MAX_CSV_BYTES = 5 * 1024 * 1024
MESSAGE_ACTIONS = {"save": None, "approve": "approved", "sent": "sent", "skip": "skipped", "draft": "draft"}


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
    flash(request, f"Imported {upload.filename}: {stats['created']} new, {stats['merged']} merged into existing "
                   f"leads, {stats['skipped']} skipped.")
    for err in stats["errors"][:5]:
        flash(request, err, "warning")
    return redirect(back)


@router.get("/c/{company_id}/leads/new")
def lead_new_page(request: Request, company_id: int) -> Response:
    company = repo.get_company(company_id)
    return render(request, "lead_new.html", {
        "company": company, "active": "leads", "title": "Add lead",
        "values": {"signal_type": "custom", "signal_strength": "50", "signal_date": date.today().isoformat()},
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
    signals, signal_total = repo.list_signals(company_id, lead_id=lead_id, include_account=True, limit=100)
    messages = sorted(repo.list_messages(company_id, lead_id=lead_id, limit=500), key=lambda m: (m.created_at, m.id))
    sent_steps = [m.step for m in messages if m.direction == "outbound" and m.status == "sent"]
    contacts = repo.contacts_at_account(lead)
    channel = services.default_channel(company, lead)
    return render(request, "lead.html", {
        "company": company, "lead": lead, "active": "leads", "title": lead.display_name,
        "signals": signals, "signal_total": signal_total, "messages": messages, "contacts": contacts,
        "default_channel": channel, "next_step": max(sent_steps, default=0) + 1,
        "claude_prompt": f"Use openberry: get the outreach context for lead {lead.id} and write a "
                         f"{channel} message, then save it.",
        "decision_maker_prompt": f"Use openberry: find the decision-maker at {lead.lead_company or 'this account'} "
                                 f"for lead {lead.id} of company {company_id} and add them as a lead.",
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
        repo.update_lead(lead_id, fields)
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
    channel = choice(_form_text(form, "channel"), MESSAGE_CHANNELS) or services.default_channel(company, lead)
    step = int_param(_form_text(form, "step"), default=1, high=10)
    engine = "ollama" if _form_text(form, "engine") == "ollama" else "template"
    signals, _ = await run_in_threadpool(lambda: repo.list_signals(company_id, lead_id=lead_id,
                                                                   include_account=True, limit=20))
    if engine == "ollama":
        previous = await run_in_threadpool(lambda: repo.list_messages(company_id, lead_id=lead_id, limit=50))
        context = outreach.outreach_context(company, lead, signals, previous, channel, step)
        try:
            subject, body = await outreach.draft_with_ollama(get_settings(), context)
        except RuntimeError as exc:
            flash(request, f"Local AI draft failed: {exc}", "error")
            return redirect(back)
    else:
        subject, body = outreach.draft_template(company, lead, signals, channel, step)
    await run_in_threadpool(lambda: repo.create_message(lead_id, body, channel=channel, subject=subject, step=step,
                                                        generated_by=engine))
    flash(request, "Draft created. Review and edit it before you send it.")
    return redirect(back)


@router.post("/c/{company_id}/leads/{lead_id}/reply")
def lead_reply(request: Request, company_id: int, lead_id: int, form: FormData = Depends(checked_form)) -> Response:
    _company_lead(company_id, lead_id)
    body = _form_text(form, "body")
    channel = choice(_form_text(form, "channel"), MESSAGE_CHANNELS) or "linkedin_dm"
    if not body:
        flash(request, "Paste the reply text first.", "error")
    else:
        repo.log_reply(lead_id, body, channel=channel)
        flash(request, "Reply logged. The lead moved to 'replied' and pending drafts were skipped.")
    return redirect(f"/c/{company_id}/leads/{lead_id}#outreach")


# --------------------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------------------

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
        return redirect(back)
    if action not in MESSAGE_ACTIONS:
        flash(request, "Unknown action.", "error")
        return redirect(back)
    edits: dict[str, Any] = {}
    if "body" in form and message.direction == "outbound":
        edits["body"] = _form_text(form, "body")
    if "subject" in form and message.direction == "outbound":
        edits["subject"] = _form_text(form, "subject")
    try:
        repo.update_message(message_id, status=MESSAGE_ACTIONS[action], **edits)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(back)
    flash(request, {
        "save": "Message saved.", "approve": "Approved. Copy it and send it from LinkedIn or your inbox.",
        "sent": "Marked as sent. The follow-up timer has started.", "skip": "Message skipped.",
        "draft": "Moved back to drafts.",
    }[action])
    return redirect(back)
