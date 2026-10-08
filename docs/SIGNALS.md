# Signal sources

A **signal** is a reason to reach out *now*: someone asked for a recommendation, complained about a competitor,
started a new job, or their company is hiring or raised money. OpenBerry collects signals from free public sources on a
schedule, and Claude adds more from anything it can browse (LinkedIn, events, websites).

You configure everything in the registration board, under step 4 "Signals & requirements". The dashboard's **Sources**
panel shows which collectors are enabled and what each one still needs.

## Built-in collectors

| Source | Needs | Emits | Lead created | Cost / terms |
|---|---|---|---|---|
| **Hacker News** (Algolia API) | keywords, competitors and/or hiring keywords | `keyword_mention`, `competitor_engagement`, `hiring` (from "Who is hiring?") | the HN user / the hiring company | Free, no key |
| **GitHub** | `owner/repo` of competitor or related repos | `competitor_engagement` (issue/PR authors), `github_star` (forks; stars only on repos you admin) | the GitHub user, enriched from their public profile | Free. `GITHUB_TOKEN` raises limits |
| **Job boards** (Greenhouse / Lever / Ashby) | `provider:token:Company` lines + hiring keywords (or ICP titles) | `hiring` | the company (account) | Free public job-board APIs |
| **Google News** | news queries, e.g. `Dubai office opening`, `raises Series A fintech` | `funding`, `job_change`, `company_news` | the company, or the person appointed | Free. Google's feed terms allow personal, non-commercial use only. Prefer publisher feeds for business use. |
| **RSS / Atom feeds** | feed URLs (e.g. `https://techcrunch.com/feed/`) + keywords to match | `funding`, `job_change`, `company_news`, `keyword_mention` | the company / person when named | Free |
| **SEC EDGAR** (US) | SEC queries + a contact e-mail (`OPENBERRY_CONTACT_EMAIL` or the company contact) | `funding` (Form D), `job_change` (8-K item 5.02) | the company | Free; SEC requires a declared User-Agent |
| **Reddit** (optional) | `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` from a Reddit app, keywords/competitors, subreddits | `keyword_mention`, `competitor_engagement` | the Reddit user | ⚠️ Since 2026 Reddit requires OAuth and an agreement for commercial use, and is closing public API access in 2027 |

How strong a signal is depends on what it says. "Anyone recommend a chauffeur service for a roadshow?" is stronger than a passing mention.
A complaint about a competitor ("switching from X, too expensive") is strongest. Each collector's module docstring
(`src/openberry/collectors/*.py`) lists its exact rules, limits and terms.

### Account-level signals

Hiring, funding, company news and SEC filings concern a *company*, so they create an **account lead**
("Acme Bank, find the decision-maker"). Any person you or Claude later add at that company inherits 60% of the account's intent.
The usual flow:

1. A job board shows *Acme Bank is hiring a Travel Manager*. An account lead appears.
2. Ask Claude: *"Find the decision-maker at the account leads of company 1 and add them."*
3. Claude researches (website, LinkedIn MCP, search) and calls `add_leads` with the person.
   The person is scored with their own fit plus the company's hiring signal.

## Signals Claude adds (no public API)

| Signal | How |
|---|---|
| Engaged with competitor posts / influencers on LinkedIn | Claude + an optional LinkedIn MCP server. The registration board lists competitor pages and influencers, and `get_prospecting_plan` turns them into a research plan. |
| Job changes of champions | Claude re-reads profiles you care about and records `job_change` with `add_signal` |
| Profile visitors / page followers | Paste LinkedIn's "who viewed your profile" or follower list into Claude. It adds them as `profile_visit`. |
| Events | Event names from the registration board. Claude finds speakers, sponsors and attendee lists with fetch or Playwright. |
| Lookalikes | Best customers from the registration board. The prospecting plan includes "companies like X" searches. |
| Anything else | `add_leads` / `add_signal` with type `custom` |

## Scoring recap

- **ICP fit (0-100):** how well title, seniority, industry, company size, location and keywords match. Criteria you left empty are ignored.
  Unknown fields get partial credit, and excluded keywords or never-contact companies disqualify.
- **Intent (0-100):** the sum of signal weights × strength, losing half its value every 21 days. It gets +15% when 2+ kinds of signal happen within 30 days.
  People get 60% of their company's account signals.
- **Score:** ½ fit + ½ intent. Once Claude has run `assess_lead`, it becomes 35% fit + 35% intent + 30% Claude.
  **Hot** ≥ 70 · **Warm** ≥ 45 · **Cold** < 45.

You can change the weight of each signal type per company (`signals.weights`, e.g. `{"hiring": 40}`) through the API or Claude.

## Adding a new source

Create `src/openberry/collectors/<name>.py` with a `Collector` subclass (see `collectors/base.py`) and add it to `ALL` in
`collectors/__init__.py`. Return `RawSignal`s. `services.ingest` does the merging, scoring and alerting.
