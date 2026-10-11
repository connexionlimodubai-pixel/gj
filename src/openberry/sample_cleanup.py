"""Sample data left by the old demo (`openberry demo` in 0.1.x), which 0.2.0 removed.

An install that loaded the demo still holds its fictional company, people and drafts. Three rules keep them
harmless:
- a lead the demo wrote (leads.source "demo") is never sent to, emailed or auto-approved, even after the user
  edited it (is_sample_lead);
- the scheduler doesn't scan the demo company while its profile is as the demo wrote it (skips_scheduled_scans);
- the dashboard (a banner on that company) and `openberry remove-sample` offer a guided removal (cleanup_plan,
  remove). Nothing is ever removed without the user asking: not on upgrade, not by the scheduler.

The demo company is recognised by the finished scan run the demo recorded (scan_runs.trigger "demo"): nothing else
writes that trigger, and only deleting the company removes it. Never by its name alone, which the user may have
changed. What may go depends on what changed since the demo wrote it:
- the whole company, when its profile and every sample lead are as the demo wrote them, nothing else was added
  to it and nothing was sent from it (CleanupPlan.whole_company_safe);
- otherwise only the untouched sample leads (their signals and drafts go with them) and the marker run. The profile,
  the sample leads that changed and every other lead stay.

The product's other modules never say "demo": its words live here (the release check greps for them).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import backups, db, repo
from .models import Company, Lead, Message, ScanRun

LEGACY_SOURCE = "demo"  # leads.source and signals.source of every row the demo wrote
MARKER_TRIGGER = "demo"  # scan_runs.trigger of the finished run the demo recorded
LEGACY_NAME_PREFIX = "Demo: "
SAMPLE_LEAD_REASON = "sample data from the old demo, not a real person"
REMOVED_COMMAND = "demo"
REMOVED_COMMAND_NOTE = ("The demo was removed in OpenBerry 0.2.0. Register your company in the dashboard "
                        "(openberry serve), or ask Claude to set it up with the onboard_company prompt.")
BANNER_TEXT = "This is the sample company from the old demo: fictional people and companies."
COMPANY_NOTE = ("This is the sample company from the old demo: fictional people and companies, never contacted. "
                "Don't work on it. The user can delete it with the banner on its dashboard.")
# A sample company the user made their own (CleanupPlan.repurposed): only the sample leads left in it are sample data.
REPURPOSED_TEXT = "This company still holds {leads} from the old demo. Sample leads are never contacted."
REPURPOSED_NOTE = " The user can remove them with the banner on its dashboard."
NO_SAMPLE_LEADS_TEXT = "No sample leads from the old demo are left in this company."
DELETE_BUTTON = "Delete sample company"
REMOVE_BUTTON = "Remove sample leads"
REMOVE_MARKER_BUTTON = "Remove sample marker"
CLI_HELP = "remove the sample company the old demo created (shows what goes first)"
NOT_FOUND = "No sample company found."

# The profile fields that show who a company is, and the SHA-256 (first 16 hex digits) of the value the demo wrote in
# each: the values themselves stay out of the product. tests/sample_data.py has them, and test_sample_cleanup checks
# these. Shown on the removal page when they still hold the demo's values, read from the database.
PROFILE_LABELS = {
    "name": "Name", "contact_name": "Contact name", "contact_email": "Contact email", "website": "Website",
    "outreach.sender_name": "Sender name", "outreach.signature": "Signature",
}
PROFILE_FINGERPRINTS = {
    "name": "892227fa7452dc4a",
    "contact_name": "d0dad2f7566ca87b",
    "contact_email": "add1754d7ac9e16a",
    "website": "5775c078c67ebad4",
    "outreach.sender_name": "94e46fa5663a30a2",
    "outreach.signature": "b67cf21e570fcbc7",
}


# --------------------------------------------------------------------------------------
# Leftover sample leads are never contacted
# --------------------------------------------------------------------------------------

def is_sample_lead(lead: Lead) -> bool:
    """A lead the old demo wrote: fictional, whoever edited it since. Its LinkedIn URL is made up, and anyone could
    register it, so nothing may ever go to it."""
    return lead.source == LEGACY_SOURCE


def sample_lead_problem(lead: Lead) -> str:
    """Why nothing may go to this lead ("" if it isn't sample data). Checked first by the agent's send queue,
    email sending and auto-approve."""
    return SAMPLE_LEAD_REASON if is_sample_lead(lead) else ""


# --------------------------------------------------------------------------------------
# The sample company
# --------------------------------------------------------------------------------------

def marker_run(company_id: int, conn: sqlite3.Connection | None = None) -> ScanRun | None:
    """The scan run the demo recorded for this company, if it is the demo company."""
    return repo.scan_run_with_trigger(company_id, MARKER_TRIGGER, conn=conn)


def is_sample_company(company_id: int, conn: sqlite3.Connection | None = None) -> bool:
    return marker_run(company_id, conn=conn) is not None


def sample_company_ids(conn: sqlite3.Connection | None = None) -> set[int]:
    return repo.company_ids_with_scan_trigger(MARKER_TRIGGER, conn=conn)


def profile_edited(company: Company) -> bool:
    """The profile was saved since the demo wrote it, or renamed. Any save counts: the profile form, Pause or
    Activate, and the switches on the Outreach page."""
    return company.updated_at != company.created_at or not company.name.startswith(LEGACY_NAME_PREFIX)


def skips_scheduled_scans(company: Company, conn: sqlite3.Connection | None = None) -> bool:
    """The scheduler leaves the demo company alone until the user changes its profile: it is fictional."""
    return not profile_edited(company) and is_sample_company(company.id, conn=conn)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _profile_value(company: Company, path: str) -> Any:
    value: Any = company
    for part in path.split("."):
        value = getattr(value, part)
    return value


def sample_profile_fields(company: Company) -> list[dict[str, str]]:
    """The profile fields that still hold the demo's values, as stored now: [{"field", "label", "value"}]."""
    return [{"field": path, "label": label, "value": str(_profile_value(company, path))}
            for path, label in PROFILE_LABELS.items()
            if fingerprint(_profile_value(company, path)) == PROFILE_FINGERPRINTS[path]]


# --------------------------------------------------------------------------------------
# What may be removed
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CleanupPlan:
    """What removing the sample data of one company would do (cleanup_plan)."""

    company: Company
    marker_run_id: int
    profile_edited: bool
    sample_fields: list[dict[str, str]]  # profile fields still holding the demo's values
    sample_lead_ids: list[int]  # untouched sample leads: removed
    touched_demo_lead_ids: list[int]  # sample leads changed since (by the user, Claude or a scan): kept
    other_lead_ids: list[int]  # leads the demo didn't write: kept
    sent_messages: list[int]  # messages sent (or being sent) from this company
    leads: dict[int, Lead] = field(repr=False)
    sample_signals: int = 0  # the signals and drafts that go with the untouched sample leads
    sample_drafts: int = 0
    counts: dict[str, int] = field(default_factory=dict)  # the company's rows: leads, messages, signals, scan_runs

    @property
    def whole_company_safe(self) -> bool:
        """Nothing in the company is the user's: deleting it loses nothing but the demo."""
        return not (self.profile_edited or self.touched_demo_lead_ids or self.other_lead_ids or self.sent_messages)

    @property
    def repurposed(self) -> bool:
        """The user made the company their own: they changed its profile (as skips_scheduled_scans counts it). Leads
        added to it don't count: 0.1.x scanned the demo company, and its profile is still the fictional one."""
        return self.profile_edited

    @property
    def sample_leads_left(self) -> int:
        """The sample leads still in the company: untouched (removed with the marker) or changed since (kept)."""
        return len(self.sample_lead_ids) + len(self.touched_demo_lead_ids)

    @property
    def token(self) -> str:
        """Changes when anything the removal decides on changes: the confirmation page sends it back, and a removal
        whose plan changed since the page was shown is refused."""
        return fingerprint([self.company.id, self.marker_run_id, self.profile_edited, self.sample_lead_ids,
                            self.touched_demo_lead_ids, self.other_lead_ids, self.sent_messages])

    def lead_lists(self, other_max: int | None = None) -> dict[str, list[Lead]]:
        """The leads themselves: "sample" (removed), "touched" and "other" (kept; at most other_max of those)."""
        return {"sample": [self.leads[i] for i in self.sample_lead_ids],
                "touched": [self.leads[i] for i in self.touched_demo_lead_ids],
                "other": [self.leads[i] for i in self.other_lead_ids[:other_max]]}

    def summary(self) -> dict[str, Any]:
        """The plan as plain data (the command line prints it)."""
        return {
            "company_id": self.company.id, "name": self.company.name, "profile_edited": self.profile_edited,
            "sample_lead_ids": self.sample_lead_ids, "touched_demo_lead_ids": self.touched_demo_lead_ids,
            "other_lead_ids": self.other_lead_ids, "sent_messages": self.sent_messages,
            "whole_company_safe": self.whole_company_safe,
        }


def banner_text(plan: CleanupPlan) -> str:
    """What the banner and the removal page say about the company: fictional, unless the user made it their own."""
    if not plan.repurposed:
        return f"{BANNER_TEXT} Nothing is ever sent to them."
    if not plan.sample_leads_left:
        return NO_SAMPLE_LEADS_TEXT
    return REPURPOSED_TEXT.format(leads=count_of(plan.sample_leads_left, "sample lead"))


def banner_button(plan: CleanupPlan) -> str:
    """The banner's button says what the removal page offers: the whole company, its sample leads, or only the
    marker (the banner then goes away)."""
    if plan.whole_company_safe:
        return DELETE_BUTTON
    return REMOVE_BUTTON if plan.sample_leads_left else REMOVE_MARKER_BUTTON


def company_note(plan: CleanupPlan) -> str:
    """The note Claude gets with the company (MCP list_companies), "" for none. A company the user made their own is
    theirs to work on: only its sample leads are left alone, and with none left there is nothing to say."""
    if not plan.repurposed:
        return COMPANY_NOTE
    return banner_text(plan) + REPURPOSED_NOTE if plan.sample_leads_left else ""


def _seeded_draft(msg: Message, marker: ScanRun) -> bool:
    """A draft the demo wrote and nobody touched: written before the demo's scan run, never edited, held or sent."""
    return (msg.direction == "outbound" and msg.status == "draft" and msg.generated_by == "template"
            and msg.updated_at == msg.created_at and not msg.auto_hold and msg.sent_at is None
            and not msg.email_state and msg.created_at <= marker.started_at)


def _untouched_sample_lead(lead: Lead, messages: list[Message], sources: set[str], marker: ScanRun) -> bool:
    """A lead exactly as the demo wrote it. Rescoring and hot-lead alerts don't change leads.updated_at, while edits
    by the user or Claude (status, notes, tags, an assessment, a merge) do."""
    return (lead.source == LEGACY_SOURCE and lead.updated_at == lead.created_at and lead.status == "new"
            and not lead.notes.strip() and not lead.tags and lead.ai_score is None and not lead.email.strip()
            and sources <= {LEGACY_SOURCE} and all(_seeded_draft(m, marker) for m in messages))


def cleanup_plan(company_id: int, conn: sqlite3.Connection | None = None) -> CleanupPlan | None:
    """What may be removed from this company: None unless it is the demo company (it has the demo's scan run)."""
    if conn is None:
        with db.connect() as c:
            return cleanup_plan(company_id, conn=c)
    marker = marker_run(company_id, conn=conn)
    if marker is None:
        return None
    company = repo.get_company(company_id, conn=conn)
    contents = repo.company_contents(company_id, conn=conn)
    by_lead: dict[int, list[Message]] = {}
    for msg in contents["messages"]:
        by_lead.setdefault(msg.lead_id, []).append(msg)
    sample, touched, other = [], [], []
    for lead in contents["leads"]:
        if not is_sample_lead(lead):
            other.append(lead.id)
        elif _untouched_sample_lead(lead, by_lead.get(lead.id, []), contents["signal_sources"].get(lead.id, set()),
                                    marker):
            sample.append(lead.id)
        else:
            touched.append(lead.id)
    sent = [m.id for m in contents["messages"] if m.sent_at is not None or m.email_state]
    return CleanupPlan(
        company=company, marker_run_id=marker.id, profile_edited=profile_edited(company),
        sample_fields=sample_profile_fields(company), sample_lead_ids=sample, touched_demo_lead_ids=touched,
        other_lead_ids=other, sent_messages=sent, leads={lead.id: lead for lead in contents["leads"]},
        sample_signals=sum(contents["signal_counts"].get(i, 0) for i in sample),
        sample_drafts=sum(len(by_lead.get(i, [])) for i in sample), counts=contents["counts"])


# --------------------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------------------

class PlanChanged(RuntimeError):
    """The sample data changed since the plan was shown (or is gone): nothing was removed."""

    def __init__(self, plan: CleanupPlan | None):
        self.plan = plan
        super().__init__("Something changed since this page was opened. Nothing was removed: check the list again."
                         if plan is not None else "There is no sample data to remove any more.")


@dataclass(frozen=True)
class Removal:
    company_deleted: bool  # True: the whole company went; False: only the untouched sample leads and the marker
    removed_leads: int
    kept_leads: int
    backup: Path | None = None


def snapshot_before_delete() -> Path:
    """Copy the database into the backups folder before the sample company is deleted, as Delete company does (same
    naming and pruning: backups.BEFORE_DELETE). A backup that fails raises, so nothing is deleted."""
    return backups.snapshot(backups.BEFORE_DELETE)


def remove(company_id: int, token: str, *, snapshot: Callable[[], Path | None] | None = None) -> Removal:
    """Remove the sample data as the plan with this token says, in one write-locked transaction (BEGIN IMMEDIATE)
    that works the plan out again first: PlanChanged if it differs (or the sample data is gone), and nothing changes.

    Whole company safe: back up the database (snapshot_before_delete), then delete the company; its rows cascade,
    while email suppressions, the mailbox state and the Google Maps usage counts are kept (they aren't the company's).
    Otherwise: delete only the untouched sample leads (their signals, keys and drafts cascade) and the demo's scan run,
    so the banner goes away. The profile and every other lead stay.
    """
    snapshot = snapshot or snapshot_before_delete
    with repo.write_locked() as c:
        plan = cleanup_plan(company_id, conn=c)
        if plan is None or plan.token != token:
            raise PlanChanged(plan)
        if plan.whole_company_safe:
            backup = snapshot()  # under the write lock: exactly what is about to be deleted
            repo.delete_company(company_id, conn=c)
            return Removal(True, len(plan.sample_lead_ids), 0, backup)
        for lead_id in plan.sample_lead_ids:
            repo.delete_lead(lead_id, conn=c)
        repo.delete_scan_run(plan.marker_run_id, conn=c)
        return Removal(False, len(plan.sample_lead_ids), plan.counts["leads"] - len(plan.sample_lead_ids))


def describe(plan: CleanupPlan) -> list[str]:
    """The plan in plain sentences, for the command line."""
    lines = [f"Company {plan.company.id}: {plan.company.name}. {BANNER_TEXT}"]
    if plan.whole_company_safe:
        rows = plan.counts
        lines.append(f"Nothing in it was changed: the whole company goes ({count_of(rows['leads'], 'lead')}, "
                     f"{count_of(rows['signals'], 'signal')}, {count_of(rows['messages'], 'draft')}).")
        return lines
    lines.append(f"Removed: {count_of(len(plan.sample_lead_ids), 'untouched sample lead')} with "
                 f"{count_of(plan.sample_signals, 'signal')} and {count_of(plan.sample_drafts, 'draft')}, "
                 "and the sample marker.")
    kept = ["the company profile" + (" (it was changed)" if plan.profile_edited else "")]
    if plan.touched_demo_lead_ids:
        kept.append(f"{count_of(len(plan.touched_demo_lead_ids), 'sample lead')} that changed since (ids "
                    f"{', '.join(map(str, plan.touched_demo_lead_ids))}): delete them yourself if you don't need them")
    if plan.other_lead_ids:
        kept.append(f"{count_of(len(plan.other_lead_ids), 'other lead')} (added by you, Claude or a scan)")
    lines.append("Kept: " + "; ".join(kept) + ".")
    if plan.sample_fields:
        lines.append("Profile fields that still hold sample values: " + "; ".join(
            f"{f['label']}: {f['value']}" for f in plan.sample_fields) + ".")
    return lines


def count_of(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
