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
    exclude_keywords, exclude_companies (never-contact list), company_types
  - `signals: SignalConfig`: enabled_types, keywords, subreddits, github_repos, job_boards,
    hiring_keywords, news_queries, rss_feeds, influencers, competitor_pages, events, lookback_days, weights
  - `outreach: OutreachConfig`: sender, tone, language, channels, calendar link, CTA, signature,
    max_followups, followup_days, mode (review | auto_draft), banned_words, extra_instructions
  - `notify: NotifyConfig`: Slack and Discord webhooks, min_score
- **Lead**: a person (`kind="person"`) or an account (`kind="account"`, company-level intent such as
  hiring or funding, with no contact found yet). People inherit 60% of their company's account-level intent.
- **lead_keys**: every identity of a lead (LinkedIn slug, email, GitHub login, name+company...). Used to merge duplicates.
- **Signal**: typed intent event (`SIGNAL_TYPES`) from a source, with strength (50 = typical), time,
  URL and a dedupe key `(company_id, source, external_id)`.
- **Message**: an outbound draft/sent message or an inbound reply (`direction`), with sequence `step`.
- **ScanRun**: one collector run with per-collector stats.

## Scoring (src/openberry/scoring.py)

`score = 0.5·ICP + 0.5·intent`, or `0.35·ICP + 0.35·intent + 0.30·Claude` once Claude has assessed the lead.
Tiers: hot ≥ 70, warm ≥ 45. ICP criteria the company left empty are skipped. Unknown fields get 40% credit.
Intent decays with a 21-day half-life. Two or more kinds of signal within 30 days add 15% (signal stacking).
Excluded keywords and never-contact companies cap the score at 15.

## Contracts between modules

- `repo.*` is the only module that writes SQL. Its functions accept an optional `conn` for one transaction.
- `services.run_scan(company_id, trigger=, sources=, client=)` is async. It returns a stats dict and also stores it on a ScanRun.
- `collectors.Collector`: `name`, `label`, `signal_types`, `requires`, `is_configured(company)`,
  `async collect(company, ctx) -> list[RawSignal]`. `RawSignal(signal=SignalIn, lead=LeadIn|None, account=str, account_domain=str)`.
  Collectors must be polite (cap requests and honour `ctx.max_items`). One bad item never fails the whole collector. Use `ctx.warn()` for soft problems.
- `web.app.create_app(settings=None) -> FastAPI`. It mounts the MCP Streamable HTTP endpoint at `/mcp` via `mcp_server.mount_http(app)` when enabled.
- `mcp_server.build_server() -> MCPServer` (mcp Python SDK 2.x, `from mcp.server.mcpserver import MCPServer`).

## Security model

- **Local mode** (no `OPENBERRY_PASSWORD`): there is no login, and it is meant for `127.0.0.1`. The dashboard, `/api` and `/mcp` only answer to
  `localhost`, IP addresses, the host of `OPENBERRY_BASE_URL` and `OPENBERRY_ALLOWED_HOSTS`, which blocks DNS-rebinding attacks.
  Browser requests that change data must carry the dashboard's CSRF token, so other websites can't drive the API.
- **Server mode:** set `OPENBERRY_PASSWORD` and `OPENBERRY_SECRET_KEY`. The dashboard then requires login and every form is CSRF-protected.
  The JSON API and `/mcp` require `Authorization: Bearer $OPENBERRY_API_TOKEN`. Session cookies are `Secure` when `OPENBERRY_BASE_URL` is https.
- `OPENBERRY_PUBLIC_REGISTRATION=true` lets anyone submit the registration form (agency intake). Such visitors can't read any data.
- Request bodies are capped at 8 MB.
- Outbound requests to user-supplied URLs (website auto-fill, RSS feeds, alert webhooks) only go to public IP addresses.
  Through MCP, Claude can only point alert webhooks at Slack or Discord.
- Claude is told to treat text from leads and public posts as data, never as instructions.
- Nothing is ever sent to LinkedIn or by email automatically. OpenBerry drafts, and a human sends.
