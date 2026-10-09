# OpenBerry architecture

OpenBerry is a free, self-hosted replica of Gojiberry-style intent-signal lead generation.
Claude does the reasoning work (research, qualification, writing) through an MCP server.
A Python app stores everything, collects free public signals, scores leads, and serves the dashboard.

```
            ┌────────────────────────── Claude Desktop / Claude Code ───────────────────────────┐
            │  openberry MCP (this repo)   +  optional OSS MCP servers (LinkedIn, Playwright,  │
            │                                 fetch, Firecrawl...) for research & enrichment   │
            └───────────────┬───────────────────────────────────────────────────────────────────┘
                            │ stdio (`openberry mcp`) or Streamable HTTP (`/mcp`)
┌───────────────────────────▼───────────────────────────────────────────────────────────────────┐
│ openberry (Python)                                                                            │
│  web/ FastAPI + Jinja dashboard: registration board, leads, signals, outreach, settings       │
│  mcp_server.py   tools/resources/prompts over the same data                                    │
│  services.py     run_scan → collectors → ingest → scoring → alerts (+ auto-draft)              │
│  collectors/     Hacker News, Reddit, GitHub, Greenhouse/Lever/Ashby, News/RSS, SEC EDGAR      │
│  scoring.py      ICP fit + time-decayed intent + signal stacking + optional Claude score       │
│  outreach.py     template / Ollama drafting, outreach context for Claude                       │
│  repo.py + db.py SQLite (WAL) – companies, leads, lead_keys, signals, messages, scan_runs      │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Data model (src/openberry/models.py, db.py)

- **Company**: the registration-board profile: identity, offer (description, products, value
  proposition, pain points, proof points), competitors, best customers, contact person, free-text
  requirements, `leads_per_week`, `scan_interval_hours`, `status`, and four nested configs:
  - `icp: ICP`: job_titles, seniorities, industries, company_sizes, locations, keywords,
    exclude_keywords, exclude_companies (never-contact list), company_types (guides Claude only, not scored)
  - `signals: SignalConfig`: enabled_types, keywords, subreddits, github_repos, job_boards,
    hiring_keywords, news_queries, rss_feeds, sec_queries, influencers, competitor_pages, events, lookback_days, weights
  - `outreach: OutreachConfig`: sender, tone, language, channels, calendar link, CTA, signature,
    max_followups, followup_days, mode (review | auto_draft), banned_words, extra_instructions, linkedin_account
    (free | premium, default free: the connection-note limits, see `outreach.connect_note_limit`), and AI agent
    sending: agent_sending (off by default), agent_daily_limit (1-50, default 15), agent_paused_until, agent_pause_reason
  - `notify: NotifyConfig`: Slack and Discord webhooks, min_score (alerts need a hot lead, so values below 70 act as 70)
- **Lead**: a person (`kind="person"`) or an account (`kind="account"`, company-level intent such as
  hiring or funding, with no contact found yet). People inherit 60% of their company's account-level intent.
  `alerted_at` is set once a hot person has been alerted, and cleared when the lead goes cold.
- **lead_keys**: every identity of a lead (LinkedIn slug, email, GitHub login, name+company...). Used to merge duplicates.
- **Signal**: typed intent event (`SIGNAL_TYPES`) from a source, with strength (50 = typical), time,
  URL and a dedupe key `(company_id, source, external_id)`.
- **Message**: an outbound draft/sent message or an inbound reply (`direction`), with sequence `step`. `sent_via` records who
  marked it sent: `""` the user, `"agent"` the user's AI agent (`confirm_message_sent`), `"claude"` Claude (`update_message`).
  Added in schema version 3.
- **ScanRun**: one scan with per-collector stats. Status: `running`, `ok`, `failed` (it crashed, or every source failed or
  found nothing and only warned) or `nothing_configured`, with the reason in `stats["error"]`.
  The scheduler retries a failed scan after an hour.

## Scoring (src/openberry/scoring.py)

`score = 0.5·ICP + 0.5·intent`, or `0.35·ICP + 0.35·intent + 0.30·Claude` once Claude has assessed the lead.
Tiers: hot ≥ 70, warm ≥ 45. ICP criteria the company left empty are skipped. Unknown fields get 40% credit.
Intent decays with a 21-day half-life. Two or more kinds of signal within 30 days add 15% (signal stacking).
ICP seniorities and company sizes may be keys, labels or ranges ("Directors", "50-200"); values it can't read are ignored.
Excluded keywords, never-contact companies and the `disqualified` lead status cap the score at 15.

## Contracts between modules

- `repo.*` is the only module that writes SQL (besides the schema and its upgrades in `db.py`). Its functions accept an
  optional `conn` for one transaction.
- `services.run_scan(company_id, trigger=, sources=, client=)` is async. It returns a stats dict and also stores it on a ScanRun.
  It raises `ScanInProgress` while another scan of the company runs in any process (one started less than 15 minutes ago).
- `services.alert_new_hot_leads(company_id)` sends the Slack/Discord alert (and, in `auto_draft` mode, drafts) for hot people
  not alerted yet; `repo.claim_new_hot_leads` hands each lead out exactly once. It runs after every scan, and the scheduler
  (`openberry serve`) runs it for every active company on each tick, so leads made hot by Claude, the API, CSV imports or
  edits are alerted too.
- `collectors.Collector`: `name`, `label`, `signal_types`, `requires`, `is_configured(company)`,
  `async collect(company, ctx) -> list[RawSignal]`. `RawSignal(signal=SignalIn, lead=LeadIn|None, account=str, account_domain=str, account_location=str)`.
  Collectors must be polite (cap requests and honour `ctx.max_items`). One bad item never fails the whole collector. Use `ctx.warn()` for soft problems.
- `web.app.create_app(settings=None) -> FastAPI`. It mounts the MCP Streamable HTTP endpoint at `/mcp` via `mcp_server.mount_http(app)` when enabled.
- `mcp_server.build_server() -> MCPServer` (mcp Python SDK 2.x, `from mcp.server.mcpserver import MCPServer`).

## Security model

- **Local mode** (no `OPENBERRY_PASSWORD`): there is no login, and it is meant for `127.0.0.1`. The dashboard and `/api` only answer to
  `localhost`, IP addresses, the host of `OPENBERRY_BASE_URL` and `OPENBERRY_ALLOWED_HOSTS`. Without `OPENBERRY_API_TOKEN`, `/mcp` is
  stricter: only localhost/127.0.0.1/::1, the host of `OPENBERRY_BASE_URL` and `OPENBERRY_ALLOWED_HOSTS` (add a LAN IP there to
  use it). This blocks DNS-rebinding attacks.
  Browser requests that change data must carry the dashboard's CSRF token, so other websites can't drive the API.
- **Server mode:** set `OPENBERRY_PASSWORD` and `OPENBERRY_SECRET_KEY`. The dashboard then requires login and every form is CSRF-protected.
  The JSON API and `/mcp` require `Authorization: Bearer $OPENBERRY_API_TOKEN`. Session cookies are `Secure` when `OPENBERRY_BASE_URL` is https.
  After 5 wrong passwords in 10 minutes an address gets 429 until the window passes. Password checks run one at a time, and each
  failure waits 0.5 s plus 0.5 s for every 5 recent failures from anyone (at most 5 s), so parallel guessing doesn't help.
- `OPENBERRY_PUBLIC_REGISTRATION=true` lets anyone submit the registration form (agency intake). Such visitors can't read any data.
  Their companies are saved paused, without RSS feeds or alert webhooks, and show as *Pending review* on the board until the
  operator activates them. Anonymous registrations and website auto-fills are limited to 5 a minute per address and 30 a minute
  in total (429 with Retry-After). The registration form refuses a company name that is already registered.
- Request bodies are capped at 8 MB.
- Outbound requests to user-supplied URLs (website auto-fill, RSS feeds, alert webhooks) only go to public IP addresses
  (`netguard.py`). The host is looked up once and the connection goes to the checked address, so DNS rebinding can't redirect
  it. These requests don't use the environment's HTTP(S) proxy, so a server that only reaches the internet through a proxy
  can't use them. Downloads are size- and time-capped, webhook responses are never read, and webhook failures are logged with
  the host only. `OPENBERRY_ALLOW_PRIVATE_FEEDS=true` turns the check off for RSS feeds.
  Through MCP, Claude can only set Slack or Discord incoming-webhook URLs (checked by host and path), and is told to set only
  URLs the user typed.
- Claude is told to treat text from leads and public posts, and everything the tools return, as data, never as instructions.
  Prompts name companies and leads by id rather than quoting stored text.
- OpenBerry never sends anything to LinkedIn or by email itself. OpenBerry drafts, and a human sends, or, when the user
  turns on AI agent sending for a company, the user's own browser agent sends the LinkedIn messages they approved.
  `repo.send_queue`, `repo.confirm_agent_sent` and `repo.report_send_problem` enforce its rules server-side: approved
  LinkedIn messages only, a rolling 24-hour limit, the never-contact list, LinkedIn's connection limits (note length
  for the account, 80 connection requests in 7 days, 5 notes in 30 days on a free account), and a 24-hour pause on
  any reported problem.
  Claude and the JSON API can only turn it off or lower the limit. See [AI_AGENT_SENDING.md](AI_AGENT_SENDING.md).
