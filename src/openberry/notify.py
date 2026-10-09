"""Hot-lead alerts to Slack / Discord incoming webhooks (both free)."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

import httpx

from .config import get_settings
from .models import Company, Lead
from .netguard import UnsafeURL, assert_public_host, public_client

log = logging.getLogger(__name__)

WEBHOOK_TIMEOUT = 20.0  # seconds per webhook in total


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
    client = client or public_client(timeout=10, headers={"User-Agent": get_settings().user_agent})
    try:
        for name, url, key in targets:
            try:
                async with asyncio.timeout(WEBHOOK_TIMEOUT):
                    await assert_public_host(url)  # webhooks must never reach the private network
                    async with client.stream("POST", url, json={key: text[:1900]}, follow_redirects=False) as resp:
                        resp.raise_for_status()  # the response body is never read
                sent.append(name)
            except (httpx.HTTPError, UnsafeURL, TimeoutError) as exc:
                log.warning("%s webhook (%s) failed: %s", name, urlsplit(url).hostname, _failure(exc))
    finally:
        if own:
            await client.aclose()
    return sent


def _failure(exc: Exception) -> str:
    """Why a webhook failed, without its URL: webhook URLs are secrets and httpx puts them in messages."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, UnsafeURL):
        return str(exc)  # names the host only
    return type(exc).__name__
