"""HTML form <-> model conversion for the registration board and the lead forms.

The registration form uses flat names for top-level fields and dotted names for the nested
configs ("icp.job_titles", "signals.job_boards", "outreach.tone", "notify.min_score").
Values are kept as the raw strings the user typed so a form with errors re-renders exactly
as submitted; `models.split_list` turns list textareas into lists during validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from starlette.datastructures import FormData

from ..models import JOB_BOARD_PROVIDERS, CompanyIn, LeadIn, OutreachConfig, SignalConfig, SignalIn, split_list

NESTED = ("icp", "signals", "outreach", "notify")


@dataclass(frozen=True)
class Field:
    name: str  # form field name; dotted for nested configs
    kind: str  # text | list | int | checks | choice | bool (a single checkbox)
    step: str  # wizard step the field lives on


STEPS: tuple[tuple[str, str], ...] = (
    ("company", "Company"),
    ("offer", "Offer"),
    ("icp", "Ideal customer"),
    ("signals", "Signals & requirements"),
    ("outreach", "Outreach & alerts"),
    ("review", "Review"),
)

FIELDS: tuple[Field, ...] = (
    *(Field(n, "text", "company") for n in (
        "website", "name", "industry", "location", "company_size", "description",
        "contact_name", "contact_email", "contact_phone")),
    *(Field(n, "text", "offer") for n in ("products", "value_proposition", "pain_points", "proof_points")),
    *(Field(n, "list", "offer") for n in ("competitors", "best_customers")),
    *(Field(f"icp.{n}", "list", "icp") for n in (
        "job_titles", "industries", "locations", "keywords", "exclude_keywords", "exclude_companies")),
    *(Field(f"icp.{n}", "checks", "icp") for n in ("seniorities", "company_sizes", "company_types")),
    Field("signals.enabled_types", "checks", "signals"),
    *(Field(f"signals.{n}", "list", "signals") for n in (
        "keywords", "subreddits", "github_repos", "job_boards", "hiring_keywords", "news_queries", "rss_feeds",
        "sec_queries", "influencers", "competitor_pages", "events") if n in SignalConfig.model_fields),
    Field("signals.lookback_days", "int", "signals"),
    Field("scan_interval_hours", "int", "signals"),
    Field("leads_per_week", "int", "signals"),
    Field("requirements", "text", "signals"),
    *(Field(f"outreach.{n}", "text", "outreach") for n in (
        "sender_name", "sender_title", "tone", "language", "call_to_action", "calendar_link", "signature",
        "extra_instructions", "followup_days")),
    Field("outreach.mode", "choice", "outreach"),
    Field("outreach.linkedin_account", "choice", "outreach"),
    Field("outreach.channels", "checks", "outreach"),
    Field("outreach.banned_words", "list", "outreach"),
    Field("outreach.max_followups", "int", "outreach"),
    # AI agent sending. The pause (agent_paused_until, agent_pause_reason) is not a form field:
    # build_company(keep=...) carries it over, so saving the profile never lifts a pause. Auto-approve
    # (outreach.auto_approve*) isn't either: it is set on the Outreach page, and repo.update_company keeps it.
    Field("outreach.agent_sending", "bool", "outreach"),
    Field("outreach.agent_daily_limit", "int", "outreach"),
    *(Field(f"notify.{n}", "text", "outreach") for n in ("slack_webhook_url", "discord_webhook_url")),
    Field("notify.min_score", "int", "outreach"),
)
FIELD_BY_NAME = {f.name: f for f in FIELDS}
HONEYPOT = "fax_number"  # hidden from people; bots that fill every input reveal themselves
CHECKED = "true"  # the value of a single checkbox (kind "bool") when it is ticked
TRUTHY = {"true", "on", "1", "yes"}


def int_bounds(model: Any, name: str, default: tuple[int, int]) -> tuple[int, int]:
    """(ge, le) of a pydantic model's int field, so forms show and check the limits the model enforces."""
    low, high = default
    for meta in model.model_fields[name].metadata:
        low, high = getattr(meta, "ge", low), getattr(meta, "le", high)
    return int(low), int(high)


AGENT_LIMIT_RANGE = int_bounds(OutreachConfig, "agent_daily_limit", (1, 50))
AUTO_APPROVE_HOURS_RANGE = int_bounds(OutreachConfig, "auto_approve_hours", (1, 72))


# --------------------------------------------------------------------------------------
# Company form
# --------------------------------------------------------------------------------------

def values_from_form(form: FormData) -> dict[str, Any]:
    """Raw submitted values keyed by field name (checkbox groups as lists)."""
    values: dict[str, Any] = {}
    for f in FIELDS:
        if f.kind == "checks":
            values[f.name] = [str(v) for v in form.getlist(f.name)]
        elif f.kind == "bool":  # an unticked checkbox is not submitted at all
            values[f.name] = CHECKED if str(form.get(f.name) or "").strip().lower() in TRUTHY else ""
        else:
            raw = form.get(f.name)
            values[f.name] = raw if isinstance(raw, str) else ""
    return values


def company_to_values(company: CompanyIn) -> dict[str, Any]:
    """Prefill values for the form from a stored profile."""
    dumped = company.model_dump(mode="json")
    values: dict[str, Any] = {}
    for f in FIELDS:
        group, _, key = f.name.rpartition(".")
        value = (dumped[group] if group else dumped).get(key)
        if f.name == "signals.job_boards":
            values[f.name] = "\n".join(f"{b['provider']}:{b['token']}:{b['company']}" for b in value or [])
        elif f.name == "outreach.followup_days":
            values[f.name] = ", ".join(str(d) for d in value or [])
        elif f.kind == "checks":
            values[f.name] = list(value or [])
        elif f.kind == "bool":
            values[f.name] = CHECKED if value else ""
        elif f.kind == "list":
            values[f.name] = "\n".join(value or [])
        else:
            values[f.name] = "" if value is None else str(value)
    return values


def default_values() -> dict[str, Any]:
    """Values for an empty registration form: the model defaults, with no name."""
    values = company_to_values(CompanyIn(name="-"))
    values["name"] = ""
    return values


def _normalize_website(url: str) -> str:
    url = url.strip()
    if url and not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) and "." in url and " " not in url:
        return "https://" + url
    return url


def _precheck(values: dict[str, Any]) -> dict[str, str]:
    """Catch input pydantic would silently drop or report cryptically."""
    errors: dict[str, str] = {}
    for n, line in enumerate(str(values.get("signals.job_boards", "")).splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 2 or not parts[1]:
            errors["signals.job_boards"] = f"Line {n}: use provider:board-token:Company, e.g. greenhouse:stripe:Stripe."
            break
        if parts[0].lower() not in JOB_BOARD_PROVIDERS:
            errors["signals.job_boards"] = f"Line {n}: provider must be one of {', '.join(JOB_BOARD_PROVIDERS)}."
            break
    for repo_name in split_list(values.get("signals.github_repos", "")):
        clean = re.sub(r"^https?://(www\.)?github\.com/", "", repo_name).strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", clean):
            errors["signals.github_repos"] = f"'{repo_name}' is not owner/repo (e.g. vercel/next.js)."
            break
    days = split_list(values.get("outreach.followup_days", ""))
    if any(not d.isdigit() for d in days):
        errors["outreach.followup_days"] = "Use whole numbers of days, e.g. 3, 7."
    email = str(values.get("contact_email", "")).strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors["contact_email"] = "Enter a valid email address."
    for f in FIELDS:
        raw = str(values.get(f.name, "")).strip() if f.kind == "int" else ""
        if raw and not re.fullmatch(r"\d+", raw):
            errors[f.name] = "Enter a whole number."
    return errors


def _payload(values: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {group: {} for group in NESTED}
    for f in FIELDS:
        group, _, key = f.name.rpartition(".")
        target = data[group] if group else data
        raw = values.get(f.name, [] if f.kind == "checks" else "")
        if f.kind == "checks":
            target[key] = list(raw)
        elif f.kind == "bool":
            target[key] = str(raw).strip().lower() in TRUTHY
        elif f.kind == "int":
            if str(raw).strip():
                target[key] = int(str(raw).strip())
        elif f.kind == "list":
            target[key] = str(raw)
        else:
            target[key] = str(raw).strip()
    data["website"] = _normalize_website(data.get("website", ""))
    for key in ("tone", "mode", "followup_days", "linkedin_account"):  # blank -> model default
        if not data["outreach"].get(key):
            data["outreach"].pop(key, None)
    return data


def _field_for_loc(loc: tuple[Any, ...]) -> tuple[str, int | None]:
    names = [str(p) for p in loc if not isinstance(p, int)]
    index = next((p for p in loc if isinstance(p, int)), None)
    if len(names) >= 2 and names[0] in NESTED:
        return f"{names[0]}.{names[1]}", index
    return (names[0] if names else "__all__"), index


def errors_by_field(exc: ValidationError) -> dict[str, str]:
    """Map pydantic errors to form field names with readable messages (first error per field)."""
    errors: dict[str, str] = {}
    for err in exc.errors(include_url=False, include_context=False):
        name, index = _field_for_loc(tuple(err["loc"]))
        msg = str(err["msg"]).removeprefix("Value error, ")
        if name == "name" and err["type"] in ("string_too_short", "missing"):
            msg = "Company name is required."
        elif index is not None:
            msg = f"Line {index + 1}: {msg}"
        if not msg.endswith("."):
            msg += "."
        errors.setdefault(name, msg)
    return errors


def _overlay(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        out[key] = _overlay(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def build_company(values: dict[str, Any], keep: CompanyIn | None = None) -> tuple[CompanyIn | None, dict[str, str]]:
    """Validate submitted values.

    With `keep` (the stored profile), the form is laid over it so fields the form does not
    show (status, signal weights, anything added to the model later) survive a save.
    """
    errors = _precheck(values)
    if errors:
        return None, errors
    data = _payload(values)
    if keep is not None:
        data = _overlay(keep.model_dump(mode="json"), data)
    try:
        return CompanyIn.model_validate(data), {}
    except ValidationError as exc:
        return None, errors_by_field(exc)


def step_of(name: str) -> str:
    """The wizard step a field is on (form-level errors belong to the first step)."""
    field = FIELD_BY_NAME.get(name)
    return field.step if field else STEPS[0][0]


def error_steps(errors: dict[str, str]) -> dict[str, str]:
    """Field name -> wizard step, so the error summary can say where each problem is and open that step."""
    return {name: step_of(name) for name in errors}


def first_error_step(errors: dict[str, str]) -> str:
    steps = set(error_steps(errors).values())
    return next((step for step, _ in STEPS if step in steps), STEPS[0][0])


# --------------------------------------------------------------------------------------
# Lead forms
# --------------------------------------------------------------------------------------

LEAD_PROFILE_FIELDS = (
    "full_name", "title", "lead_company", "company_domain", "industry", "company_size", "location",
    "linkedin_url", "email", "phone", "website", "github_username", "twitter", "profile_url", "bio",
)
SIGNAL_FIELDS = ("signal_type", "signal_title", "signal_url", "signal_summary", "signal_date", "signal_strength")


def _text(form: FormData, name: str) -> str:
    raw = form.get(name)
    return raw.strip() if isinstance(raw, str) else ""


def signal_from_form(form: FormData) -> tuple[SignalIn | None, dict[str, str]]:
    """Optional manual signal from the signal_* fields (None when title and URL are both empty)."""
    title, url = _text(form, "signal_title"), _text(form, "signal_url")
    if not title and not url:
        return None, {}
    strength = _text(form, "signal_strength") or "50"
    data = {
        "type": _text(form, "signal_type") or "custom", "title": title, "url": url,
        "summary": _text(form, "signal_summary"), "occurred_at": _text(form, "signal_date") or None,
        "strength": strength, "source": "manual",
    }
    try:
        return SignalIn.model_validate(data), {}
    except ValidationError as exc:
        errors: dict[str, str] = {}
        for err in exc.errors(include_url=False, include_context=False):
            key = {"occurred_at": "signal_date", "strength": "signal_strength"}.get(str(err["loc"][0]), "signal_title")
            errors.setdefault(key, str(err["msg"]).removeprefix("Value error, ") + ".")
        return None, errors


def lead_from_form(form: FormData) -> tuple[LeadIn | None, dict[str, str], dict[str, str]]:
    """Returns (lead, values, errors) for the 'Add lead' form."""
    values = {name: _text(form, name) for name in (*LEAD_PROFILE_FIELDS, "notes", "tags", *SIGNAL_FIELDS)}
    errors: dict[str, str] = {}
    if not values["full_name"] and not values["lead_company"]:
        errors["full_name"] = "Enter a person's name, or at least a company for an account-level lead."
    signal, signal_errors = signal_from_form(form)
    errors.update(signal_errors)
    if errors:
        return None, values, errors
    data: dict[str, Any] = {name: values[name] for name in LEAD_PROFILE_FIELDS}
    data.update(notes=values["notes"], tags=values["tags"], source="manual", signals=[signal] if signal else [])
    try:
        return LeadIn.model_validate(data), values, {}
    except ValidationError as exc:
        for err in exc.errors(include_url=False, include_context=False):
            errors.setdefault(str(err["loc"][0]), str(err["msg"]) + ".")
        return None, values, errors


def profile_from_form(form: FormData) -> dict[str, str]:
    """Editable profile fields for repo.update_lead (only fields present in the form)."""
    return {name: _text(form, name) for name in LEAD_PROFILE_FIELDS if name in form}
