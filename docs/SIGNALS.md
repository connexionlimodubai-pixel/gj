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
| **RSS / Atom feeds** | feed URLs (e.g. `https://techcrunch.com/feed/`) + keywords, competitors or news queries to match | `funding`, `job_change`, `company_news`, `keyword_mention` | the company / person when named | Free |
| **SEC EDGAR** (US) | SEC queries + a contact e-mail (`OPENBERRY_CONTACT_EMAIL` or the company contact) | `funding` (Form D), `job_change` (8-K item 5.02) | the company | Free; SEC requires a declared User-Agent |
| **Reddit** (optional) | `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` from a Reddit app, keywords/competitors, subreddits | `keyword_mention`, `competitor_engagement` | the Reddit user | ⚠️ Since 2026 Reddit requires OAuth and an agreement for commercial use, and is closing public API access in 2027 |
| **Google Maps businesses** (Places API Text Search) | Google Maps searches + a Google Maps API key (API keys page) | `business_search` (strength 10, weight 10) | the business (account) with the email and phone from its own website | Free up to Google's 1,000 searches a month; OpenBerry stops at 900. Only Place IDs are kept (Google's terms) |

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

### Google Maps businesses (a prospect list, not intent)

For businesses that don't post online but are good customers, such as hotels, event planners, DMCs, law firms and
corporate offices. Add searches under **Signals & requirements → Google Maps searches**, one per line, as you'd type them
in Google Maps: `event management companies in Dubai`, `law firms in DIFC`. Put the place in the search. Then add your
Google Maps API key on the dashboard's **API keys** page (or set `OPENBERRY_GOOGLE_PLACES_KEY`).

How a search runs:

- Google's [Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search) returns
  pages of up to 20 businesses. OpenBerry reads up to 3 pages (60 businesses) per search and at most 10 pages per scan.
  Each search runs at most once a week. Each page is one search on Google's bill: the first 1,000 a month are free, and
  OpenBerry stops at 900 (`OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT`, or the API keys page). The page shows how many were
  used this month.
- For each business, OpenBerry opens its own website: the homepage, plus up to 2 contact or about pages when the
  homepage lacks an email or a phone number. It respects each site's robots.txt, only visits public addresses, and caps
  the size and time of every page (1.5 MB, 10 seconds, 20 seconds per business).
- Emails: `mailto:` links and the site's schema.org data for any domain except junk (noreply, example addresses, image
  file names, error trackers); addresses in the text, including `info [at] acme [dot] ae`, only on the site's own domain.
  Addresses hidden by an email-protection service (Cloudflare) are not decoded: the site chose to hide them. Role
  addresses such as `info@` come first. Phone numbers come from `tel:` links and the schema.org data.
- Each business becomes an account lead with the name, website, email, phone and description its website publishes,
  the place from your search ("Dubai") and a "Google Maps" link. Businesses without a website, or with only a social
  page, are skipped and counted in the scan's stats, as are sites that are unreachable or keep robots out.
- A business found again (by another search, or a week later) is not visited again. It merges with leads found by other
  sources by name, domain or Place ID. A lead you delete doesn't come back: use **Disqualified** to keep one but ignore it.

Google's terms forbid storing Google Maps content ("Customer will not ... pre-fetch, index, store, reshare, or rehost
Google Maps Content outside the services"), except Place IDs, which "you can ... store ... indefinitely"
([Places API policies](https://developers.google.com/maps/documentation/places/web-service/policies)). So the Place
ID is all OpenBerry keeps from Google; everything else comes from the business's own website. Whether a list of
businesses for outreach suits your use of Google Maps Platform is for you to check in Google's terms.

Being on Google Maps is not intent: a `business_search` signal is worth little (weight 10, strength 10) and never counts
toward signal stacking, so these leads are scored mostly on ICP fit and show as cold or warm. Use them as a prospect
list, and ask Claude to find the right person at the ones that fit.

Before you email them: these are business contact addresses, but email providers block mailboxes that send to people
who didn't opt in, and anti-spam laws apply. Send few, personal, relevant emails and honour opt-outs.

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
- **Intent (0-100):** each signal is worth its type weight × strength/50 (at most 2×), halved every 21 days.
  People get 60% of their company's account signals. The values are added, and the sum gets +15% when 2+ kinds of signal
  happened in the last 30 days. The result is squashed to 0-100 as 100·(1 − e^(−sum/60)), so extra signals add less and less:
  one fresh hiring signal of typical strength (weight 25) gives 34, and 49 with weight 40. A type weighted 0 is ignored.
- **Score:** ½ fit + ½ intent. Once Claude has run `assess_lead`, it becomes 35% fit + 35% intent + 30% Claude.
  **Hot** ≥ 70 · **Warm** ≥ 45 · **Cold** < 45. Excluded keywords, never-contact companies and the *Disqualified*
  pipeline status cap the score at 15.

Default weights: competitor engagement and profile visits 35, funding and job changes 30, topic posts and hiring 25,
GitHub stars/forks and influencer engagement 20, events, company news and other signals 15, business searches 10 (they
never count toward signal stacking).
You can change the weight of each signal type per company (`signals.weights`, e.g. `{"hiring": 40}`) through the API or Claude.
Sending `weights` replaces the whole map, so include every override you want to keep.

## Adding a new source

Create `src/openberry/collectors/<name>.py` with a `Collector` subclass (see `collectors/base.py`) and add it to `ALL` in
`collectors/__init__.py`. Return `RawSignal`s. `services.ingest` does the merging and scoring, and the scan then
alerts on people who turned hot.
