"""Hot-lead alerts to Slack / Discord incoming webhooks (both free)."""

from __future__ import annotations

import logging

import httpx

from .config import get_settings
from .models import Company, Lead

log = logging.getLogger(__name__)


def format_hot_leads(company: Company, leads: list[Lead]) -> str:
    lines = [f"🔥 {len(leads)} new hot lead(s) for {company.name}"]
    for lead in leads[:10]:
        who = lead.full_name or lead.lead_company
        role = f" — {lead.title}" if lead.title else ""
        at = f" @ {lead.lead_company}" if lead.lead_company and lead.full_name else ""
        link = lead.linkedin_url or lead.profile_url
        lines.append(f"• {who}{role}{at} (score {lead.score})" + (f" {link}" if link else ""))
    if len(leads) > 10:
        lines.append(f"…and {len(leads) - 10} more")
    return "\n".join(lines)


async def notify_hot_leads(company: Company, leads: list[Lead], client: httpx.AsyncClient | None = None) -> list[str]:
    """Send an alert for leads at or above the company's alert threshold. Returns channels notified."""
    leads = [lead for lead in leads if lead.score >= company.notify.min_score]
    targets = []
    if company.notify.slack_webhook_url:
        targets.append(("slack", company.notify.slack_webhook_url, "text"))
    if company.notify.discord_webhook_url:
        targets.append(("discord", company.notify.discord_webhook_url, "content"))
    if not leads or not targets:
        return []
    text = format_hot_leads(company, leads)
    sent: list[str] = []
    own = client is None
    client = client or httpx.AsyncClient(timeout=10, headers={"User-Agent": get_settings().user_agent})
    try:
        for name, url, key in targets:
            try:
                resp = await client.post(url, json={key: text[:1900]})
                resp.raise_for_status()
                sent.append(name)
            except httpx.HTTPError as exc:
                log.warning("%s webhook failed: %s", name, exc)
    finally:
        if own:
            await client.aclose()
    return sent
