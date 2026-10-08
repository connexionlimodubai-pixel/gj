"""Outreach drafting.

Three ways to get a message, from best to most basic:
  1. Claude, through the MCP server: `get_outreach_context` -> Claude writes -> `save_outreach_message`.
  2. A free local LLM via Ollama (dashboard "Draft with local AI" when OPENBERRY_OLLAMA_URL is set).
  3. A deterministic template that references the lead's strongest signal (always available).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .config import Settings
from .models import Company, Lead, Message, Signal

LINKEDIN_CONNECT_LIMIT = 300   # LinkedIn connection-request note limit (characters)
LINKEDIN_DM_SOFT_LIMIT = 600

CHANNEL_GUIDANCE = {
    "linkedin_connect": f"LinkedIn connection request note. Hard limit {LINKEDIN_CONNECT_LIMIT} characters. "
                        "No pitch, no links: reference the signal and give a reason to connect.",
    "linkedin_dm": f"LinkedIn direct message, under {LINKEDIN_DM_SOFT_LIMIT} characters, 3-5 short lines, "
                   "one soft call to action.",
    "email": "Cold email: a short specific subject line (max 7 words, no clickbait) and a 60-120 word body: "
             "personal opener tied to the signal, one-sentence value proposition, one call to action, signature.",
    "other": "Short, personal message.",
}

FOLLOWUP_GUIDANCE = ("This is follow-up #{n}. Do not repeat the first message; add one new angle "
                     "(a relevant result, a question, or a resource) and keep it shorter than the previous one.")


def first_name(lead: Lead) -> str:
    name = (lead.full_name or "").strip()
    return name.split()[0] if name else "there"


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:-") + "…"


def signal_hook(signal: Signal | None, lead: Lead) -> str:
    """A short, natural phrase referencing why we're reaching out now."""
    if signal is None:
        return ""
    title = _short(signal.title, 70)
    company = lead.lead_company or "your team"
    hooks = {
        "competitor_engagement": f"your take on “{title}”",
        "keyword_mention": f"your post “{title}”",
        "hiring": f"that {company} is hiring ({title})",
        "funding": f"the news about {company}'s funding",
        "job_change": "your new role",
        "github_star": f"that you starred {title}" if title else "your GitHub activity",
        "influencer_engagement": f"your comment on “{title}”",
        "profile_visit": "that you checked out our profile",
        "event": f"that you're attending {title}",
        "company_news": f"the recent news about {company}",
    }
    return hooks.get(signal.type, f"“{title}”" if title else "")


def _value_line(company: Company) -> str:
    vp = company.value_proposition or company.description or company.products
    return _short(vp, 160)


def draft_template(company: Company, lead: Lead, signals: list[Signal], channel: str = "linkedin_dm",
                   step: int = 1) -> tuple[str, str]:
    """Return (subject, body). Subject is empty for LinkedIn channels."""
    o = company.outreach
    top = max(signals, key=lambda s: (s.strength, s.occurred_at), default=None)
    hook = signal_hook(top, lead)
    name = first_name(lead)
    sender = o.sender_name or "the team"
    value = _value_line(company)
    cta = o.call_to_action or "Open to a quick chat?"
    if o.calendar_link and channel != "linkedin_connect":
        cta = f"{cta} {o.calendar_link}"

    if channel == "linkedin_connect":
        opener = f"Hi {name}, saw {hook}." if hook else f"Hi {name},"
        body = f"{opener} I'm {sender} at {company.name}; we help {_audience(company)}. Would love to connect!"
        if len(body) > LINKEDIN_CONNECT_LIMIT:
            body = f"{opener} I'm {sender} at {company.name}. Would love to connect!"
        return "", _short(body, LINKEDIN_CONNECT_LIMIT)

    if step > 1:
        body = (f"Hi {name}, following up on my last note. "
                f"{'Teams like ' + lead.lead_company + ' often' if lead.lead_company else 'Teams like yours often'} "
                f"tell us {(_short(company.pain_points, 120) or 'this is worth a look').rstrip('.')}. "
                f"{cta}")
        subject = f"Re: {lead.lead_company or name}" if channel == "email" else ""
        return subject, _sign(body, o.signature, sender, channel)

    lines = [f"Hi {name},", ""]
    if hook:
        lines.append(f"I noticed {hook} and thought it was worth reaching out.")
    if value:
        lines.append(f"At {company.name} {value[0].lower() + value[1:] if value else ''}".rstrip(". ") + ".")
    lines += ["", cta]
    body = _sign("\n".join(lines), o.signature, sender, channel)
    subject = ""
    if channel == "email":
        subject = _short(f"{name}, quick idea for {lead.lead_company}" if lead.lead_company
                         else f"Quick idea, {name}", 60)
    return subject, body


def _audience(company: Company) -> str:
    titles = company.icp.job_titles[:2]
    industries = company.icp.industries[:1]
    who = " & ".join(titles) if titles else "teams"
    if industries:
        who += f" in {industries[0]}"
    return who


def _sign(body: str, signature: str, sender: str, channel: str) -> str:
    sig = signature.strip() or sender
    if channel == "email" or signature.strip():
        return f"{body}\n\n{sig}"
    return f"{body}\n\n{sender}"


def outreach_context(company: Company, lead: Lead, signals: list[Signal], previous: list[Message],
                     channel: str = "linkedin_dm", step: int = 1) -> dict[str, Any]:
    """Everything an LLM needs to write one personalised message, as plain data."""
    o = company.outreach
    return {
        "channel": channel,
        "step": step,
        "channel_guidance": CHANNEL_GUIDANCE.get(channel, CHANNEL_GUIDANCE["other"])
        + (" " + FOLLOWUP_GUIDANCE.format(n=step - 1) if step > 1 else ""),
        "sender": {
            "name": o.sender_name, "title": o.sender_title, "company": company.name,
            "website": company.website, "signature": o.signature, "calendar_link": o.calendar_link,
        },
        "style": {"tone": o.tone, "language": o.language, "call_to_action": o.call_to_action,
                  "banned_words": o.banned_words, "extra_instructions": o.extra_instructions},
        "offer": {
            "description": company.description, "products": company.products,
            "value_proposition": company.value_proposition, "pain_points": company.pain_points,
            "proof_points": company.proof_points, "competitors": company.competitors,
        },
        "lead": {
            "id": lead.id, "kind": lead.kind, "name": lead.full_name, "first_name": first_name(lead),
            "title": lead.title, "company": lead.lead_company, "industry": lead.industry,
            "location": lead.location, "bio": _short(lead.bio, 600), "linkedin_url": lead.linkedin_url,
            "score": lead.score, "tier": lead.tier, "why_scored": lead.score_reasons[:8],
            "ai_rationale": lead.ai_rationale,
        },
        "signals": [
            {"type": s.type, "label": s.label, "title": s.title, "summary": _short(s.summary, 300), "url": s.url,
             "when": s.occurred_at.date().isoformat()}
            for s in sorted(signals, key=lambda s: s.occurred_at, reverse=True)[:6]
        ],
        "previous_messages": [
            {"direction": m.direction, "channel": m.channel, "step": m.step, "status": m.status,
             "subject": m.subject, "body": m.body, "date": (m.sent_at or m.created_at).date().isoformat()}
            for m in sorted(previous, key=lambda m: m.created_at)[-6:]
        ],
        "rules": [
            "Reference the most relevant signal naturally; never say you are tracking or scraping them.",
            "One clear call to action. No buzzwords, no fake familiarity, no false claims.",
            f"Write in {o.language or 'English'} with a {o.tone or 'friendly'} tone.",
            *([f"Never use these words/phrases: {', '.join(o.banned_words)}."] if o.banned_words else []),
            "If previous_messages contains an inbound reply, answer that reply instead of pitching again.",
            "Return only the message text (and a subject line for email).",
        ],
    }


def build_llm_prompt(context: dict[str, Any]) -> str:
    import json

    wants_subject = context["channel"] == "email"
    return (
        "You write short, personalised B2B outreach messages.\n"
        f"Channel rules: {context['channel_guidance']}\n"
        "Rules:\n- " + "\n- ".join(context["rules"]) + "\n\n"
        f"Context (JSON):\n{json.dumps(context, ensure_ascii=False, indent=1)}\n\n"
        + ("Reply exactly in this format:\nSubject: <subject line>\n\n<email body>"
           if wants_subject else "Reply with the message text only.")
    )


def parse_llm_reply(text: str, channel: str) -> tuple[str, str]:
    text = (text or "").strip().strip("`").strip()
    subject = ""
    m = re.match(r"(?is)^\s*subject\s*:\s*(.+?)\n(.*)$", text)
    if m:
        subject, text = m.group(1).strip(), m.group(2).strip()
    if channel == "linkedin_connect":
        text = _short(text, LINKEDIN_CONNECT_LIMIT)
    return (subject if channel == "email" else ""), text


async def draft_with_ollama(settings: Settings, context: dict[str, Any],
                            client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    """Draft with a local Ollama model. Raises RuntimeError if Ollama isn't configured/reachable."""
    if not settings.ollama_url:
        raise RuntimeError("Ollama is not configured (set OPENBERRY_OLLAMA_URL, e.g. http://localhost:11434)")
    own = client is None
    client = client or httpx.AsyncClient(timeout=120)
    try:
        resp = await client.post(f"{settings.ollama_url}/api/generate", json={
            "model": settings.ollama_model, "prompt": build_llm_prompt(context), "stream": False,
            "options": {"temperature": 0.6},
        })
        resp.raise_for_status()
        reply = resp.json().get("response", "")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc
    finally:
        if own:
            await client.aclose()
    subject, body = parse_llm_reply(reply, context["channel"])
    if not body:
        raise RuntimeError("Ollama returned an empty reply")
    return subject, body
