"""Sample data left by the old demo (`openberry demo` in 0.1.x), which 0.2.0 removed.

An install that loaded the demo still holds its fictional company, people and drafts. Two rules keep them
harmless: a lead the demo wrote (leads.source "demo") is never sent to, emailed or auto-approved, even after
the user edited it; and the dashboard offers a guided, safe removal (see cleanup_plan).
"""

from __future__ import annotations

from .models import Lead

LEGACY_SOURCE = "demo"  # leads.source and signals.source of every row the demo wrote
SAMPLE_LEAD_REASON = "sample data from the old demo, not a real person"
REMOVED_COMMAND = "demo"
REMOVED_COMMAND_NOTE = ("The demo was removed in OpenBerry 0.2.0. Register your company in the dashboard "
                        "(openberry serve), or ask Claude to set it up with the onboard_company prompt.")


def is_sample_lead(lead: Lead) -> bool:
    """A lead the old demo wrote: fictional, whoever edited it since. Its LinkedIn URL is made up, and anyone could
    register it, so nothing may ever go to it."""
    return lead.source == LEGACY_SOURCE


def sample_lead_problem(lead: Lead) -> str:
    """Why nothing may go to this lead ("" if it isn't sample data). Checked first by the agent's send queue,
    email sending and auto-approve."""
    return SAMPLE_LEAD_REASON if is_sample_lead(lead) else ""
