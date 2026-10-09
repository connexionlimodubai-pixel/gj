"""Pydantic models shared by the dashboard, the JSON API, the MCP server and the collectors."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------

# type -> (label shown in the UI, default weight 0-100 used by intent scoring)
SIGNAL_TYPES: dict[str, tuple[str, int]] = {
    "competitor_engagement": ("Engaged with a competitor", 35),
    "keyword_mention": ("Posted about your topic", 25),
    "hiring": ("Hiring for a relevant role", 25),
    "funding": ("Raised funding", 30),
    "job_change": ("New job or promotion", 30),
    "github_star": ("Starred or forked a relevant GitHub repo", 20),
    "influencer_engagement": ("Engaged with a niche influencer", 20),
    "profile_visit": ("Visited your profile or website", 35),
    "event": ("Attending a relevant event", 15),
    "company_news": ("Company in the news", 15),
    "custom": ("Other signal", 15),
}

SIGNAL_SOURCES = (
    "hackernews", "reddit", "github", "greenhouse", "lever", "ashby",
    "google_news", "rss", "sec_edgar", "linkedin", "web", "manual", "claude", "csv", "demo",
)

SENIORITIES: dict[str, str] = {
    "founder": "Founder / Owner",
    "c_level": "C-level (CEO, CTO, CFO...)",
    "vp": "VP",
    "head": "Head of",
    "director": "Director",
    "manager": "Manager",
    "senior": "Senior individual contributor",
    "entry": "Entry level",
}

COMPANY_SIZES = ("1-10", "11-50", "51-200", "201-1000", "1001-5000", "5000+")

LEAD_STATUSES = ("new", "qualified", "contacted", "replied", "meeting", "won", "lost", "disqualified")
MESSAGE_CHANNELS = ("linkedin_connect", "linkedin_dm", "email", "other")
MESSAGE_STATUSES = ("draft", "approved", "sent", "replied", "skipped", "received")
MESSAGE_DIRECTIONS = ("outbound", "inbound")
COMPANY_TYPES = ("startup", "smb", "mid-market", "enterprise", "agency", "public sector", "nonprofit")
TONES = ("friendly", "professional", "casual", "direct")
TIERS = ("hot", "warm", "cold")
# ScanRun.status -> label. 'failed': it crashed, or every source failed or came back empty with problems.
SCAN_STATUSES: dict[str, str] = {
    "running": "Running",
    "ok": "Ok",
    "failed": "Failed",
    "nothing_configured": "No sources configured",
}
JOB_BOARD_PROVIDERS = ("greenhouse", "lever", "ashby")
# Channels an AI agent may send through the send queue (email is always sent by the user).
AGENT_CHANNELS = ("linkedin_connect", "linkedin_dm")
# Message.sent_via: who recorded the send. "" = the user (dashboard, API, CSV), "agent" = confirm_message_sent,
# "claude" = Claude marked it sent with update_message (counts toward the agent's daily limit too).
SENT_VIA = ("", "agent", "claude")
AGENT_PAUSE_REASON_MAX = 500


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def split_list(value: Any) -> list[str]:
    """Accept a list or a comma/newline separated string; strip, drop blanks, dedupe."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[\n,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            parts.extend(split_list(item) if isinstance(item, str) and ("\n" in item) else [item])
    else:
        parts = [value]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


StrList = Annotated[list[str], BeforeValidator(split_list)]


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


class _Model(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


# --------------------------------------------------------------------------------------
# Company registration
# --------------------------------------------------------------------------------------

class ICP(_Model):
    """Ideal customer profile: who should be a lead."""

    job_titles: StrList = Field(default_factory=list, description="Target job titles/keywords, e.g. 'Head of Sales', 'Founder'")
    seniorities: StrList = Field(default_factory=list, description=f"Any of: {', '.join(SENIORITIES)}")
    industries: StrList = Field(default_factory=list)
    company_sizes: StrList = Field(default_factory=list, description=f"Any of: {', '.join(COMPANY_SIZES)}")
    locations: StrList = Field(default_factory=list, description="Countries, regions or cities")
    keywords: StrList = Field(default_factory=list, description="Topics/interests that indicate fit (matched against bio/title/company)")
    exclude_keywords: StrList = Field(default_factory=list, description="Disqualify leads mentioning these (e.g. 'student', 'recruiter')")
    exclude_companies: StrList = Field(default_factory=list, description="Never-contact list: existing customers, partners, competitors")
    company_types: StrList = Field(default_factory=list, description=f"Any of: {', '.join(COMPANY_TYPES)}")


class JobBoard(_Model):
    provider: Literal["greenhouse", "lever", "ashby"]
    token: str = Field(min_length=1, max_length=200, description="Board token / site name, e.g. 'stripe' for boards.greenhouse.io/stripe")
    company: str = Field(default="", max_length=200, description="Display name of the target account")

    @field_validator("token")
    @classmethod
    def _clean_token(cls, v: str) -> str:
        v = v.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", v):
            raise ValueError("job board token may only contain letters, digits, '.', '_' and '-'")
        return v


def parse_job_boards(value: Any) -> list[dict[str, str]] | Any:
    """Accept 'greenhouse:stripe:Stripe' lines (from a textarea) as well as dicts."""
    if isinstance(value, str):
        value = split_list(value)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                parts = [p.strip() for p in item.split(":")]
                if len(parts) >= 2:
                    out.append({"provider": parts[0].lower(), "token": parts[1], "company": ":".join(parts[2:]) or parts[1]})
            else:
                out.append(item)
        return out
    return value


class SignalConfig(_Model):
    """What to watch. Each list feeds one or more collectors."""

    enabled_types: StrList = Field(default_factory=lambda: list(SIGNAL_TYPES), description="Signal types to track")
    keywords: StrList = Field(default_factory=list, description="Topics to monitor on HN/Reddit/news, e.g. 'corporate chauffeur'")
    subreddits: StrList = Field(default_factory=list, description="Subreddits to watch (without r/)")
    github_repos: StrList = Field(default_factory=list, description="owner/repo of competitor or related repos: issue authors and forkers become leads (stargazers only for repos you admin, with GITHUB_TOKEN)")
    job_boards: Annotated[list[JobBoard], BeforeValidator(parse_job_boards)] = Field(default_factory=list)
    hiring_keywords: StrList = Field(default_factory=list, description="Job titles at target accounts that signal need, e.g. 'SDR', 'Travel Manager'")
    news_queries: StrList = Field(default_factory=list, description="Google News queries, e.g. 'raises Series A fintech'")
    rss_feeds: StrList = Field(default_factory=list, description="Any RSS/Atom feed URLs to scan for keywords")
    sec_queries: StrList = Field(default_factory=list, description="SEC EDGAR full-text queries (US companies). Form D funding filings match names, places, people and industry labels (e.g. 'Other Technology', 'Texas'); 8-K executive changes match topical phrases (e.g. 'logistics software')")
    influencers: StrList = Field(default_factory=list, description="LinkedIn profile URLs whose post engagers Claude should check")
    competitor_pages: StrList = Field(default_factory=list, description="Competitor LinkedIn/company pages whose engagers Claude should check")
    events: StrList = Field(default_factory=list, description="Events/webinars whose attendees are good leads")
    lookback_days: int = Field(default=14, ge=1, le=90)
    weights: dict[str, int] = Field(default_factory=dict, description="Override default signal weights (0-100)")

    @field_validator("subreddits")
    @classmethod
    def _clean_subs(cls, v: list[str]) -> list[str]:
        return [s.removeprefix("/").removeprefix("r/").strip("/") for s in v if s]

    @field_validator("github_repos")
    @classmethod
    def _clean_repos(cls, v: list[str]) -> list[str]:
        out = []
        for repo in v:
            repo = re.sub(r"^https?://(www\.)?github\.com/", "", repo).strip("/")
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                out.append(repo)
        return out

    @field_validator("weights")
    @classmethod
    def _clamp_weights(cls, v: dict[str, int]) -> dict[str, int]:
        return {k: max(0, min(100, int(w))) for k, w in v.items() if k in SIGNAL_TYPES}


class OutreachConfig(_Model):
    sender_name: str = ""
    sender_title: str = ""
    tone: str = "friendly"
    language: str = "English"
    channels: StrList = Field(default_factory=lambda: ["linkedin", "email"])
    calendar_link: str = ""
    call_to_action: str = "Open to a quick 15-minute chat next week?"
    signature: str = ""
    max_followups: int = Field(default=2, ge=0, le=6)
    followup_days: Annotated[list[int], BeforeValidator(lambda v: [int(x) for x in split_list(v)] if isinstance(v, str) else v)] = Field(
        default_factory=lambda: [3, 7], description="Days to wait after each sent message before the next follow-up is due")
    mode: Literal["review", "auto_draft"] = Field(
        default="review", description="review: you draft on demand. auto_draft: a draft is created for every new hot lead, however it turned hot (scan, Claude, import or edit). Nothing is ever sent automatically.")
    banned_words: StrList = Field(default_factory=list, description="Words/phrases messages must never use")
    extra_instructions: str = Field(default="", description="Anything Claude must respect when writing messages")
    # LinkedIn's own limits on connection-request notes depend on the account (LinkedIn help a563153, a6239760):
    # outreach.connect_note_limit() and the AI agent's send queue apply the matching ones.
    linkedin_account: Literal["free", "premium"] = Field(
        default="free", description="The LinkedIn account messages are sent from. free (Basic): LinkedIn lets you add "
                                    "a personal note to at most 5 connection requests a month, each at most 200 "
                                    "characters. premium: a note on every request, up to 300 characters.")
    # AI agent sending: an MCP-capable browser agent in the user's own browser sends the LinkedIn messages the
    # user approved, through the send queue (repo.send_queue). These four are changed in the dashboard only.
    agent_sending: bool = Field(
        default=False, description="Let an AI agent in your own browser send the LinkedIn messages you approved")
    agent_daily_limit: int = Field(
        default=15, ge=1, le=50, description="Most LinkedIn messages the agent may send in any 24 hours")
    agent_paused_until: datetime | None = Field(
        default=None, description="Agent sending is paused until then (the agent reported a problem)")
    agent_pause_reason: str = Field(default="", description="The problem the agent reported")

    @field_validator("linkedin_account", mode="before")
    @classmethod
    def _account_word(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("agent_paused_until")
    @classmethod
    def _utc_pause(cls, v: datetime | None) -> datetime | None:
        return v.replace(tzinfo=timezone.utc) if v is not None and v.tzinfo is None else v

    @field_validator("agent_pause_reason")
    @classmethod
    def _short_reason(cls, v: str) -> str:
        return v[:AGENT_PAUSE_REASON_MAX]


class NotifyConfig(_Model):
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""
    min_score: int = Field(default=70, ge=0, le=100)

    @field_validator("slack_webhook_url", "discord_webhook_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("webhook URLs must start with https://")
        return v


class CompanyIn(_Model):
    """Everything a company enters on the registration board."""

    name: str = Field(min_length=1, max_length=200)
    website: str = ""
    industry: str = ""
    location: str = ""
    company_size: str = ""
    description: str = Field(default="", description="What the company does")
    products: str = Field(default="", description="Products / services offered")
    value_proposition: str = ""
    pain_points: str = Field(default="", description="Problems you solve for customers")
    proof_points: str = Field(default="", description="Results, case studies, notable clients to cite")
    competitors: StrList = Field(default_factory=list)
    best_customers: StrList = Field(default_factory=list, description="Your best customers (names or domains): seeds for lookalike prospecting")
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    requirements: str = Field(default="", description="Free-text requirements: goals, volumes, constraints")
    leads_per_week: int = Field(default=50, ge=1, le=5000)
    scan_interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    status: Literal["active", "paused"] = "active"
    icp: ICP = Field(default_factory=ICP)
    signals: SignalConfig = Field(default_factory=SignalConfig)
    outreach: OutreachConfig = Field(default_factory=OutreachConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)


class Company(CompanyIn):
    id: int
    last_scan_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------------------
# Leads, signals, messages
# --------------------------------------------------------------------------------------

class SignalIn(_Model):
    type: str = "custom"
    title: str = ""
    summary: str = ""
    url: str = ""
    source: str = "manual"
    external_id: str = ""
    strength: int = Field(default=50, ge=0, le=100, description="How strong this occurrence is: 50 = typical, 100 = very strong")
    occurred_at: Annotated[datetime | None, BeforeValidator(_blank_to_none)] = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        return v if v in SIGNAL_TYPES else "custom"

    @field_validator("occurred_at")
    @classmethod
    def _not_in_future(cls, v: datetime | None) -> datetime | None:
        # A future date (an event's date, a typo) would never decay and would rank as the latest activity.
        if v is None:
            return None
        now = datetime.now(timezone.utc)
        return now if (v if v.tzinfo else v.replace(tzinfo=timezone.utc)) > now else v


class LeadIn(_Model):
    """A person (or, when no name is known, an account) to add or update."""

    full_name: str = ""
    title: str = ""
    lead_company: str = ""
    company_domain: str = ""
    industry: str = ""
    company_size: str = ""
    location: str = ""
    linkedin_url: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    github_username: str = ""
    twitter: str = ""
    profile_url: str = ""
    bio: str = ""
    source: str = "manual"
    notes: str = ""
    tags: StrList = Field(default_factory=list)
    signals: list[SignalIn] = Field(default_factory=list)


class Lead(_Model):
    id: int
    company_id: int
    kind: Literal["person", "account"] = "person"
    full_name: str = ""
    title: str = ""
    lead_company: str = ""
    company_domain: str = ""
    industry: str = ""
    company_size: str = ""
    location: str = ""
    linkedin_url: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    github_username: str = ""
    twitter: str = ""
    profile_url: str = ""
    bio: str = ""
    source: str = "manual"
    icp_score: int = 0
    intent_score: int = 0
    ai_score: int | None = None
    ai_rationale: str = ""
    score: int = 0
    tier: str = "cold"
    score_reasons: list[str] = Field(default_factory=list)
    status: str = "new"
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    last_signal_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def display_name(self) -> str:
        if self.full_name:
            return self.full_name
        return f"{self.lead_company or 'Unknown company'} (find decision-maker)"


class Signal(_Model):
    id: int
    company_id: int
    lead_id: int | None
    type: str
    source: str
    external_id: str
    title: str = ""
    summary: str = ""
    url: str = ""
    strength: int = 50
    occurred_at: datetime
    created_at: datetime

    @property
    def label(self) -> str:
        return SIGNAL_TYPES.get(self.type, SIGNAL_TYPES["custom"])[0]


class Message(_Model):
    id: int
    company_id: int
    lead_id: int
    direction: str = "outbound"
    channel: str = "linkedin_dm"
    step: int = 1
    subject: str = ""
    body: str
    status: str = "draft"
    generated_by: str = "template"
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None = None
    sent_via: str = Field(default="", description="'' = marked sent by the user, 'agent' = the AI agent sent it, "
                                                   "'claude' = Claude marked it sent")


class ScanRun(_Model):
    id: int
    company_id: int
    trigger: str = "manual"
    status: str = "running"
    started_at: datetime
    finished_at: datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
