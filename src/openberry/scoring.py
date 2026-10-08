"""Lead scoring: ICP fit (who they are) + intent (what they just did) + optional Claude judgement.

Everything here is a pure function so it is easy to test and to explain in the UI.
Reasons are short strings with a one-character prefix the dashboard turns into an icon:
    "+" match   "-" miss   "?" unknown   "!" disqualifier   "*" signal   "AI" Claude's assessment
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import COMPANY_SIZES, ICP, SENIORITIES, SIGNAL_TYPES

# Weights of each ICP criterion. Criteria the company left empty are skipped and the
# rest re-normalised, so a sparse ICP still produces a meaningful 0-100 score.
ICP_WEIGHTS = {
    "title": 35,
    "seniority": 15,
    "industry": 15,
    "company_size": 10,
    "location": 10,
    "keywords": 15,
}
UNKNOWN_CREDIT = 0.4          # partial credit when we simply don't know the field yet
ACCOUNT_SIGNAL_FACTOR = 0.6   # company-level signals (hiring, funding) count a bit less for a person
INTENT_HALF_LIFE_DAYS = 21.0  # a signal loses half its value every 3 weeks
INTENT_SATURATION = 60.0      # higher = more signals needed to approach 100
STACKING_BONUS = 0.15         # +15% when 2+ distinct signal types happened within STACKING_WINDOW_DAYS
STACKING_WINDOW_DAYS = 30
HOT_THRESHOLD = 70
WARM_THRESHOLD = 45

# Common location aliases so "UAE" matches "Dubai, United Arab Emirates" etc.
LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "uae": ("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah"),
    "united arab emirates": ("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah"),
    "gcc": ("uae", "united arab emirates", "dubai", "abu dhabi", "saudi", "riyadh", "jeddah", "qatar", "doha",
            "kuwait", "bahrain", "oman", "muscat"),
    "ksa": ("ksa", "saudi arabia", "riyadh", "jeddah", "dammam"),
    "saudi arabia": ("ksa", "saudi arabia", "riyadh", "jeddah", "dammam"),
    "usa": ("usa", "united states", "u.s.", "new york", "san francisco", "california", "texas"),
    "us": ("usa", "united states", "u.s.", "new york", "san francisco", "california", "texas"),
    "united states": ("usa", "united states", "u.s."),
    "uk": ("uk", "united kingdom", "england", "london", "scotland", "wales", "manchester"),
    "united kingdom": ("uk", "united kingdom", "england", "london", "scotland", "wales"),
    "europe": ("europe", "eu", "germany", "france", "spain", "italy", "netherlands", "uk", "united kingdom",
               "london", "paris", "berlin", "amsterdam", "madrid", "ireland", "sweden"),
    "india": ("india", "bangalore", "bengaluru", "mumbai", "delhi", "pune", "hyderabad"),
}

SENIORITY_PATTERNS: list[tuple[str, str]] = [
    ("founder", r"\b(co-?founder|founder|owner|proprietor)\b"),
    ("c_level", r"\b(ceo|cto|cfo|coo|cmo|cro|cio|cpo|chief\b|president|managing director|general manager)\b"),
    ("vp", r"\b(vp|vice president|svp|evp)\b"),
    ("head", r"\bhead of\b|\bhead\b"),
    ("director", r"\bdirector\b"),
    ("manager", r"\b(manager|lead|supervisor|coordinator)\b"),
    ("entry", r"\b(intern|internship|trainee|junior|student|graduate)\b"),
    ("senior", r"\b(senior|sr\.?|principal|staff)\b"),
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]+", _norm(text)))


def phrase_in(phrase: str, text: str) -> bool:
    """True if every word of `phrase` appears in `text` (order-insensitive, whole words)."""
    words = _tokens(phrase)
    return bool(words) and words <= _tokens(text)


def any_phrase(phrases: list[str], text: str) -> str | None:
    for phrase in phrases:
        if phrase_in(phrase, text):
            return phrase
    return None


def infer_seniority(title: str) -> str | None:
    # "Executive Assistant to the CEO" is not C-level: drop "to the <boss>" before matching.
    t = re.sub(r"\b(to|for) (the )?[a-z &]+$", "", _norm(title)).strip()
    if not t:
        return None
    for level, pattern in SENIORITY_PATTERNS:
        if re.search(pattern, t):
            return level
    return None


def size_bucket(size: str) -> str | None:
    """Map '51-200', '120', '120 employees', '10k+' ... to one of COMPANY_SIZES."""
    s = _norm(size).replace(",", "")
    if not s:
        return None
    if s in COMPANY_SIZES:
        return s
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k)?", s)
    if not m:
        return None
    n = float(m.group(1)) * (1000 if m.group(2) else 1)
    # For ranges like "51-200" use the upper bound's bucket only if the lower is ambiguous.
    for bucket, upper in (("1-10", 10), ("11-50", 50), ("51-200", 200), ("201-1000", 1000), ("1001-5000", 5000)):
        if n <= upper:
            return bucket
    return "5000+"


def location_matches(target: str, location: str) -> bool:
    loc = _norm(location)
    if not loc:
        return False
    aliases = LOCATION_ALIASES.get(_norm(target), (_norm(target),))
    return any(phrase_in(alias, loc) or alias in loc for alias in aliases)


@dataclass
class SignalPoint:
    """The bits of a signal scoring needs."""

    type: str
    strength: int
    occurred_at: datetime
    title: str = ""
    account_level: bool = False


@dataclass
class ScoreResult:
    icp_score: int
    intent_score: int
    score: int
    tier: str
    reasons: list[str] = field(default_factory=list)
    disqualified: bool = False


@dataclass
class LeadFacts:
    """The lead fields ICP scoring reads (a Lead or LeadIn both fit via from_obj)."""

    kind: str = "person"
    title: str = ""
    lead_company: str = ""
    industry: str = ""
    company_size: str = ""
    location: str = ""
    bio: str = ""

    @classmethod
    def from_obj(cls, obj: object) -> "LeadFacts":
        return cls(**{k: (getattr(obj, k, "") or "") for k in cls.__dataclass_fields__})


def icp_fit(lead: LeadFacts, icp: ICP) -> tuple[int, list[str], bool]:
    """Return (score 0-100, reasons, disqualified)."""
    reasons: list[str] = []
    earned = 0.0
    possible = 0.0
    haystack = " ".join([lead.title, lead.bio, lead.lead_company, lead.industry])

    excluded = any_phrase(icp.exclude_keywords, " ".join([lead.title, lead.bio]))
    if excluded:
        return 0, [f"! Excluded keyword '{excluded}'"], True
    if lead.lead_company:
        company_norm = _norm(lead.lead_company)
        blocked = next((c for c in icp.exclude_companies
                        if _norm(c) == company_norm or phrase_in(c, lead.lead_company)), None)
        if blocked:
            return 0, [f"! On never-contact list ('{blocked}')"], True

    def criterion(key: str, configured: bool, known: bool, matched: str | None, label: str, miss: str) -> None:
        nonlocal earned, possible
        if not configured:
            return
        weight = ICP_WEIGHTS[key]
        possible += weight
        if matched:
            earned += weight
            reasons.append(f"+ {label}")
        elif not known:
            earned += weight * UNKNOWN_CREDIT
            reasons.append(f"? {miss}")
        else:
            reasons.append(f"- {miss}")

    is_person = lead.kind != "account"

    if is_person:
        title_hit = any_phrase(icp.job_titles, lead.title)
        criterion("title", bool(icp.job_titles), bool(lead.title), title_hit,
                  f"Title matches '{title_hit}'", "Title unknown" if not lead.title else f"Title '{lead.title}' not targeted")

        seniority = infer_seniority(lead.title)
        wanted = {s.lower() for s in icp.seniorities}
        sen_hit = SENIORITIES.get(seniority, seniority) if seniority in wanted else None
        criterion("seniority", bool(wanted), seniority is not None, sen_hit,
                  f"Seniority: {sen_hit}", "Seniority unknown" if seniority is None
                  else f"Seniority '{SENIORITIES.get(seniority, seniority)}' not targeted")

    ind_text = " ".join([lead.industry, lead.bio, lead.lead_company])
    ind_hit = any_phrase(icp.industries, ind_text)
    criterion("industry", bool(icp.industries), bool(lead.industry), ind_hit,
              f"Industry matches '{ind_hit}'", "Industry unknown" if not lead.industry
              else f"Industry '{lead.industry}' not targeted")

    bucket = size_bucket(lead.company_size)
    size_hit = bucket if bucket and bucket in icp.company_sizes else None
    criterion("company_size", bool(icp.company_sizes), bucket is not None, size_hit,
              f"Company size {size_hit}", "Company size unknown" if bucket is None
              else f"Company size {bucket} not targeted")

    loc_hit = next((loc for loc in icp.locations if location_matches(loc, lead.location)), None)
    criterion("location", bool(icp.locations), bool(lead.location), loc_hit,
              f"Located in {loc_hit}", "Location unknown" if not lead.location
              else f"Location '{lead.location}' not targeted")

    kw_hit = any_phrase(icp.keywords, haystack)
    criterion("keywords", bool(icp.keywords), bool(lead.bio or lead.title), kw_hit,
              f"Mentions '{kw_hit}'", "No profile text to match keywords" if not (lead.bio or lead.title)
              else "No ICP keywords in profile")

    if possible == 0:
        return 50, ["? No ICP criteria configured; fit is neutral"], False
    return round(100 * earned / possible), reasons, False


def type_weight(signal_type: str, overrides: dict[str, int] | None = None) -> int:
    if overrides and signal_type in overrides:
        return overrides[signal_type]
    return SIGNAL_TYPES.get(signal_type, SIGNAL_TYPES["custom"])[1]


def _age_days(occurred_at: datetime, now: datetime) -> float:
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - occurred_at).total_seconds() / 86400)


def humanize_age(days: float) -> str:
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 60:
        return f"{int(days)}d ago"
    return f"{int(days // 30)}mo ago"


def intent(signals: list[SignalPoint], weights: dict[str, int] | None = None,
           now: datetime | None = None) -> tuple[int, list[str]]:
    """Sum of decayed signal values, squashed into 0-100.

    `strength` is how strong this occurrence is (50 = typical, 100 = twice as strong)
    and multiplies the per-type weight.
    """
    now = now or datetime.now(timezone.utc)
    total = 0.0
    scored: list[tuple[float, str]] = []
    for s in signals:
        age = _age_days(s.occurred_at, now)
        value = type_weight(s.type, weights) * min(2.0, max(0.0, s.strength / 50))
        value *= 0.5 ** (age / INTENT_HALF_LIFE_DAYS)
        if s.account_level:
            value *= ACCOUNT_SIGNAL_FACTOR
        total += value
        label = SIGNAL_TYPES.get(s.type, SIGNAL_TYPES["custom"])[0]
        where = " (company)" if s.account_level else ""
        scored.append((value, f"* {label}{where}, {humanize_age(age)}"))
    recent_types = {s.type for s in signals if _age_days(s.occurred_at, now) <= STACKING_WINDOW_DAYS}
    stacked = len(recent_types) >= 2
    if stacked:
        total *= 1 + STACKING_BONUS
    score = round(100 * (1 - math.exp(-total / INTENT_SATURATION)))
    scored.sort(key=lambda x: -x[0])
    reasons = [text for _, text in scored[:5]]
    if stacked:
        reasons.insert(0, f"* Signal stacking: {len(recent_types)} kinds of intent in {STACKING_WINDOW_DAYS} days")
    if len(scored) > 5:
        reasons.append(f"* +{len(scored) - 5} more signals")
    if not signals:
        reasons.append("? No intent signals yet")
    return score, reasons


def tier_for(score: int) -> str:
    if score >= HOT_THRESHOLD:
        return "hot"
    if score >= WARM_THRESHOLD:
        return "warm"
    return "cold"


def combine(icp_score: int, intent_score: int, ai_score: int | None) -> int:
    if ai_score is None:
        return round(0.5 * icp_score + 0.5 * intent_score)
    return round(0.35 * icp_score + 0.35 * intent_score + 0.30 * ai_score)


def score_lead(lead: object, icp: ICP, signals: list[SignalPoint], weights: dict[str, int] | None = None,
               ai_score: int | None = None, ai_rationale: str = "", now: datetime | None = None) -> ScoreResult:
    facts = LeadFacts.from_obj(lead)
    icp_score, icp_reasons, disqualified = icp_fit(facts, icp)
    intent_score, intent_reasons = intent(signals, weights, now)
    score = combine(icp_score, intent_score, ai_score)
    reasons = icp_reasons + intent_reasons
    if ai_score is not None:
        reasons.append(f"AI {ai_score}/100" + (f": {ai_rationale[:160]}" if ai_rationale else ""))
    if disqualified:
        score = min(score, 15)
    return ScoreResult(icp_score, intent_score, score, tier_for(score), reasons, disqualified)
