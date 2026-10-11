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
│  services.py     run_scan → collectors → ingest → scoring → alerts (+ auto-draft, auto-approve)│
│  collectors/     HN, Reddit, GitHub, Greenhouse/Lever/Ashby, News/RSS, SEC EDGAR, Google Maps  │
│  scoring.py      ICP fit + time-decayed intent + signal stacking + optional Claude score       │
│  outreach.py     template / Ollama drafting, outreach context for Claude                       │
│  mailer.py       email from the user's own mailbox: SMTP (TLS) to send, IMAP (read-only)       │
│  scheduler.py    scans, alerts, auto-approve; email_loop sends approved emails, reads inbox   │
│  repo.py + db.py SQLite (WAL) – companies, leads, lead_keys, signals, messages, scan_runs,     │
│                  api_usage, place_searches, place_ids, email_suppressions, email_events,       │
│                  email_mailbox                                                                 │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Data model (src/openberry/models.py, db.py)

- **Company**: the registration-board profile: identity, offer (description, products, value
  proposition, pain points, proof points), competitors, best customers, contact person, free-text
  requirements, `leads_per_week`, `scan_interval_hours`, `status`, and four nested configs:
  - `icp: ICP`: job_titles, seniorities, industries, company_sizes, locations, keywords,
    exclude_keywords, exclude_companies (never-contact list), company_types (guides Claude only, not scored)
  - `signals: SignalConfig`: enabled_types, keywords, subreddits, github_repos, job_boards,
    hiring_keywords, news_queries, rss_feeds, sec_queries, places_queries (Google Maps searches, one per line),
    influencers, competitor_pages, events, lookback_days, weights
  - `outreach: OutreachConfig`: sender, tone, language, channels, calendar link, CTA, signature,
    max_followups, followup_days, mode (review | auto_draft), banned_words, extra_instructions, linkedin_account
    (free | premium, default free: the connection-note limits, see `outreach.connect_note_limit`), AI agent
    sending: agent_sending (off by default), agent_daily_limit (1-50, default 15), agent_paused_until, agent_pause_reason,
    and auto-approve: auto_approve (off by default), auto_approve_hours (the review window, 1-72, default 2) and
    auto_approve_since (when the user turned it on, made the window shorter, or activated the paused company), and
    email sending (`EMAIL_SETTINGS`): email_sending (off by default), email_daily_limit (1-100, default 30),
    email_gap_minutes (default 5), email_days / email_start_hour / email_end_hour (the sending hours on the computer's
    clock, default Monday to Friday 09:00-18:00), email_footer (added under every email when it is sent),
    email_send_auto_approved (off) and the pause OpenBerry sets (email_paused_until, email_pause_reason). The
    profile form shows none of the pauses, auto-approve or email sending, and `repo.update_company` keeps them as
    stored when it is given a whole profile.
  - `notify: NotifyConfig`: Slack and Discord webhooks, min_score (alerts need a hot lead, so values below 70 act as 70)
- **Lead**: a person (`kind="person"`) or an account (`kind="account"`, company-level intent such as
  hiring or funding, with no contact found yet). People inherit 60% of their company's account-level intent.
  `alerted_at` is set once a hot person has been alerted, and cleared when the lead goes cold.
- **lead_keys**: every identity of a lead (LinkedIn slug, email, GitHub login, name+company...). Used to merge duplicates.
- **Signal**: typed intent event (`SIGNAL_TYPES`) from a source, with strength (50 = typical), time,
  URL and a dedupe key `(company_id, source, external_id)`.
- **Message**: an outbound draft/sent message or an inbound reply (`direction`), with sequence `step`. `sent_via` records who
  marked it sent: `""` the user, `"agent"` the user's AI agent (`confirm_message_sent`), `"claude"` Claude (`update_message`).
  Added in schema version 3. Schema version 4 added `auto_hold` (1 = the user, or Claude for them, held this draft: auto-approve
  never approves it; setting an approved message back to draft on purpose holds it too, and so does a new LinkedIn profile
  or a lead leaving the pipeline, for its LinkedIn drafts and lapsed approvals), `approved_via` (`"auto"` = auto-approve
  approved it, `""` = a person did; cleared whenever it becomes a draft or a person approves it) and `auto_blocked`
  (1 = auto-approve found a reason to leave this draft alone; once the reason goes away its window starts again).
  Schema version 5 added email sending: `email_state` (`""`, `claimed`, `data` = the mail server is receiving it,
  `sent`, `failed`, `unknown` = may have been sent, never retried automatically, `bounced`), `email_message_id` (the
  Message-ID header, set at the first claim and kept), `email_to`, `email_claim` and `email_claimed_at` (the sending
  worker's token and time), `email_attempts` and `send_error`. `sent_via = "smtp"`: OpenBerry sent it from the user's
  mailbox. Replies found in the inbox are inbound messages with `generated_by = "inbox"`.
- **email_suppressions**: addresses never emailed again, for every company (`opt_out`, `bounce`, `manual`). They
  outlive the lead. **email_events**: what the sender and the inbox check did (reply, opt_out, bounce, soft_bounce,
  policy_reject, auto_reply, send_failed, unknown, resolved); `inbound_key` (the inbox message's Message-ID, else its
  folder and UID) handles each inbox message once. **email_mailbox**: one row per From address: its pause, failures
  in a row, the last inbox check and the last UID read in each folder. Never a password.
- **api_usage**: paid API calls per service and calendar month (UTC), the monthly cap on Google Maps searches.
- **place_searches**: when each company last read every page of each Google Maps search (a search runs at most once a
  week). **place_ids**: the Google Place IDs a company already handled (`added = 1`: became a lead, never added again,
  written by `services.ingest` with the lead so an interrupted scan loses nothing; `added = 0`: skipped, checked again
  after 30 days). Google's terms allow storing Place IDs and nothing else from Places.
  The three tables are created with `CREATE TABLE IF NOT EXISTS` (no schema version change).
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
- `repo.auto_approve_due(company_id, now=None, conn=None)` approves the drafts whose review window has passed, when the
  company is active and has auto-approve on: a draft is due at `max(updated_at, auto_approve_since) + auto_approve_hours`,
  so an edit restarts its window and turning auto-approve on never approves a backlog at once. It never approves a held
  draft, a lead whose status is replied/meeting/won/lost/disqualified or who replied, a lead on the never-contact list or
  matching excluded keywords (the agent queue's own checks), a step already sent to the lead (the agent's check too), a
  connection note too long for the LinkedIn account, a draft with a banned word or an unfilled placeholder
  (`outreach.unfilled_placeholder`), an email without a subject, the first `linkedin_dm` after a connection request (no one
  knows whether it was accepted), or a second message to the same lead (one at a time, step by step, then in the order
  written). A draft it leaves alone is flagged (`auto_blocked`); when the reason goes away, its `updated_at` is set to
  now, so it waits a full window again rather than being approved at once. The approvals are one `BEGIN IMMEDIATE`
  transaction of compare-and-set UPDATEs (still a draft, not held, same `updated_at`), and `repo.update_message` takes the
  same lock, so an edit at the same moment wins: it either lands first, or sees the approval and makes the message a draft
  again. It runs on every scheduler tick (`services.auto_approve_active_companies`, one company's error never stops the
  others), at the end of `run_scan`, at the start of the MCP `get_send_queue`, when the Outreach page or a lead page
  opens, and when Claude lists or reads drafts (MCP `list_outreach`, `get_lead`, `save_outreach_message`,
  `update_message`), so every block anyone is shown is flagged (without the write lock unless there is something to
  write). `repo.auto_approve_states(company, messages)` describes each draft (waiting with its time, held, blocked with
  the reason, or off) in three queries for the dashboard and Claude.
- `collectors.Collector`: `name`, `label`, `signal_types`, `requires`, `is_configured(company)`,
  `async collect(company, ctx) -> list[RawSignal]`. `RawSignal(signal=SignalIn, lead=LeadIn|None, account=str, account_domain=str, account_location=str)`.
  Collectors must be polite (cap requests and honour `ctx.max_items`). One bad item never fails the whole collector. Use `ctx.warn()` for soft problems,
  and `ctx.count(name)` for what happened (stored as `stats.collectors.<name>.counts`). `Collector.timeout_seconds` overrides the shared
  120-second limit, and `Collector.usage()` reports paid-API usage for the dashboard and `get_company_profile`.
- Email sending (`docs/EMAIL_SENDING.md`): `scheduler.email_loop` runs in the dashboard process only (`openberry serve`,
  the desktop app; never `openberry mcp`), next to the scheduler, every minute or at once when the Send now button sets
  `app.state.email_wake`. Each turn: `repo.expire_email_claims`, `services.check_inbox` every 5 minutes, then
  `services.send_next_email`. `repo.claim_next_email` checks every rule under `BEGIN IMMEDIATE` (the switches, the
  mailbox and its inbox check in the last 30 minutes, nothing in flight, the daily limits, the minutes since the
  mailbox's last email, the sending hours, `repo._email_send_problem` for each email) and claims one email with a
  compare-and-set; the returned `mailer.EmailJob` is a snapshot. The recipient is the normalized address: the
  never-twice rules, a reply and the follow-up wait count over every lead (any company) with that address
  (`repo._email_threads`). `mailer.send` goes step by step and calls `repo.mark_email_data` just before DATA, which
  checks every rule again (so a change made meanwhile counts) and refuses, so the server gets RSET, if the email may no
  longer go;
  `repo.record_email_sent` (keyed on the Message-ID) or `repo.record_email_failure` records the outcome. A claim
  older than 10 minutes goes back to the queue; one stuck in DATA becomes `unknown`. `repo.record_inbound` records
  what came back once (a reply through `repo.log_reply`, an opt-out through `repo.record_opt_out(automatic=True)`, a
  bounce); a folder's position moves on only after each of its messages was recorded, so a database error fails the
  check (sending waits) and the message is read again. A refused IMAP login pauses the mailbox like a refused SMTP
  one (`repo.pause_mailbox_login`): no automatic check until the settings are saved or Try again is clicked. The
  mailbox settings live in the data folder's `.env` (`config.MAILBOX_SETTINGS`, written by the Mailbox page through
  `envfile.save_settings`); `mailer.Mailbox.from_settings` reads them.
- `repo.reserve_api_call(service, limit)` counts one paid call before it is made, unless the month's limit is reached; it runs under
  `BEGIN IMMEDIATE`, so processes never go over the limit together.
- `config.refresh_saved_settings()` applies settings another process saved to the data folder's `.env` (one `stat()` when nothing
  changed); scans, the collector overview and the API keys page call it, so no restart is needed. `envfile.save_settings()` writes
  them, for the names in `config.DASHBOARD_SETTINGS` only (the dashboard's API keys page).
- `web.app.create_app(settings=None) -> FastAPI`. It mounts the MCP Streamable HTTP endpoint at `/mcp` via `mcp_server.mount_http(app)` when enabled.
- `mcp_server.build_server() -> MCPServer` (mcp Python SDK 2.x, `from mcp.server.mcpserver import MCPServer`).

## Security model

- **Local mode** (no `OPENBERRY_PASSWORD`): there is no login, and it is meant for `127.0.0.1`. The dashboard and `/api` only answer to
  `localhost`, IP addresses, the host of `OPENBERRY_BASE_URL` and `OPENBERRY_ALLOWED_HOSTS`. Without `OPENBERRY_API_TOKEN`, `/mcp` is
  stricter: only localhost/127.0.0.1/::1, the host of `OPENBERRY_BASE_URL` and `OPENBERRY_ALLOWED_HOSTS`. This blocks DNS-rebinding
  attacks from websites. It is not access control: another computer can send any `Host`, so using `/mcp` from one needs the token.
  Browser requests that change data must carry the dashboard's CSRF token, so other websites can't drive the API.
- **Server mode:** set `OPENBERRY_PASSWORD` and `OPENBERRY_SECRET_KEY`. The dashboard then requires login and every form is CSRF-protected.
  The JSON API and `/mcp` require `Authorization: Bearer $OPENBERRY_API_TOKEN`. Session cookies are `Secure` when `OPENBERRY_BASE_URL` is https.
  After 5 wrong passwords in 10 minutes an address gets 429 until the window passes. Password checks run one at a time, and each
  failure waits 0.5 s plus 0.5 s for every 5 recent failures from anyone (at most 5 s), so parallel guessing doesn't help.
  After 20 wrong API tokens in 10 minutes (`/api` and `/mcp` together) an address gets 429 with `Retry-After`.
- **Start-up checks** (`config.start_check`): `openberry serve` on an address other than loopback refuses to start
  without `OPENBERRY_PASSWORD` (unless `OPENBERRY_ALLOW_NO_PASSWORD=true`) or with one shorter than 12 characters. It warns
  when the secret key or the API token is missing or shorter than 32 characters, and when `OPENBERRY_BASE_URL` is `http://`
  on a network address. On 127.0.0.1 nothing changes. `openberry mcp --http` on such an address refuses to start without
  `OPENBERRY_API_TOKEN`, even with `OPENBERRY_ALLOW_NO_PASSWORD=true` (`config.mcp_start_check`), and warns about one shorter
  than 32 characters. `OPENBERRY_TRUSTED_PROXIES` sets uvicorn's `forwarded_allow_ips`.
  The access log is off (it would hold every lead search's query string).
- **On a server:** the compose `caddy` profile puts Caddy in front (the `Caddyfile` serves `OPENBERRY_BASE_URL` and
  forwards to `openberry:8000`), with a Let's Encrypt certificate, `http://` redirected to `https://`, and OpenBerry's
  own port published on 127.0.0.1 only. Caddy gets `OPENBERRY_BASE_URL` and no other setting. The steps are in the
  README, "Run it on a server".
- **Files:** `openberry` and the desktop app run with umask 077. The data folder, `logs/` and `backups/` are created with
  mode 0700, and the database, its `-wal` and `-shm` and every backup are set to 0600 (Linux, macOS, Docker's `/data`).
  At start (`config.private_files`) an existing data folder (`OPENBERRY_HOME`) is set to 0700 and its `.env` to 0600 when
  the user owns them, so a folder an older version made is private too. A symlink is followed. Only a folder and a
  regular file are changed: a device such as `/dev/null`, a FIFO, a socket or a shared folder with the sticky bit
  such as `/tmp` is left alone. A failed chmod never stops the start.
- **Headers:** a strict CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy` and
  `Cross-Origin-Opener-Policy: same-origin` on every response, error pages included; `Cache-Control: no-store` on everything
  but `/static`; HSTS when `OPENBERRY_BASE_URL` is https. `/api/openapi.json` needs a login or the API token in server mode.
- `OPENBERRY_PUBLIC_REGISTRATION=true` lets anyone submit the registration form (agency intake). Such visitors can't read any data.
  Their companies are saved paused, without RSS feeds or alert webhooks, and show as *Pending review* on the board until the
  operator activates them. Anonymous registrations and website auto-fills are limited to 5 a minute per address and 30 a minute
  in total (429 with Retry-After). The registration form refuses a company name that is already registered.
- Request bodies are capped at 8 MB.
- API keys page: logged-in dashboard users only (local mode: this machine). Keys go to the data folder's `.env` with mode 0600,
  are never shown back (the last 4 characters at most) and are never readable or settable through `/api` or MCP. A setting in
  the real environment, or in a `.env` file read first, wins and is shown read-only. Business websites found on Google Maps
  are fetched like RSS feeds: public addresses only, size and time caps (the size cap applies after decompression), plus
  robots.txt. Everything that reads a page or a robots.txt file runs in a worker thread and in linear time (no
  backtracking regular expressions on text a website controls), so one hostile site can't freeze the dashboard.
- Outbound requests to user-supplied URLs (website auto-fill, RSS feeds, alert webhooks) only go to public IP addresses
  (`netguard.py`). The host is looked up once and the connection goes to the checked address, so DNS rebinding can't redirect
  it. These requests don't use the environment's HTTP(S) proxy, so a server that only reaches the internet through a proxy
  can't use them. Downloads are size- and time-capped, webhook responses are never read, and webhook failures are logged with
  the host only. `OPENBERRY_ALLOW_PRIVATE_FEEDS=true` turns the check off for RSS feeds.
  Through MCP, Claude can only set Slack or Discord incoming-webhook URLs (checked by host and path), and is told to set only
  URLs the user typed.
- Claude is told to treat text from leads and public posts, and everything the tools return, as data, never as instructions.
  Prompts name companies and leads by id rather than quoting stored text.
- OpenBerry never sends anything to LinkedIn itself. OpenBerry drafts, and a human sends, or, when the user
  turns on AI agent sending for a company, the user's own browser agent sends the approved LinkedIn messages.
  `repo.send_queue`, `repo.confirm_agent_sent` and `repo.report_send_problem` enforce its rules server-side: approved
  LinkedIn messages only, a rolling 24-hour limit, the never-contact list, LinkedIn's connection limits (note length
  for the account, 80 connection requests in 7 days, 5 notes in 30 days on a free account), and a 24-hour pause on
  any reported problem.
  Claude and the JSON API can only turn it off or lower the limit. See [AI_AGENT_SENDING.md](AI_AGENT_SENDING.md).
- Auto-approve is off for every company until the user turns it on, on the dashboard's Outreach page (CSRF-protected).
  Claude (`update_company`, `register_company`) and the JSON API can only turn it off or make the review window longer:
  they can't turn it on, shorten the window or set `auto_approve_since`, and anonymous public registrations are saved with
  it off. Claude can hold a draft (`update_message` with `auto_hold=true`) but never release a hold: a new version of a
  held draft (`save_outreach_message`) is held too, also when the held one was skipped first. Activating a paused
  company (from anywhere) restarts every window, like turning auto-approve on. With agent sending also on, the agent may
  send what auto-approve approved, so every agent rule above still applies to it.
- Email sending is off for every company until the user turns it on, on the Outreach page's Email sending card
  (CSRF-protected), which needs a connected mailbox, a good inbox check, a footer with "unsubscribe" and no
  placeholder, and the warnings acknowledged. Claude and the JSON API can only make it safer (turn it off, lower the
  limit, space emails further apart, narrow the hours, stop auto-approved emails being sent;
  `outreach.email_setting_refusals`); anonymous public registrations are saved with it off. Only the dashboard process
  sends; `openberry mcp` never sends or reads mail, and `get_email_status` (MCP) and `GET /api/companies/{id}/email-status`
  are read-only. The mailbox password is written to the data folder's `.env` (mode 0600) by the Mailbox page only: never
  stored in SQLite, never shown back, never returned by MCP or the API, never logged (`repr=False` on every secret
  setting). SMTP uses port 465 or 587 only, IMAP 993 only, always encrypted with certificates and host names checked
  (TLS 1.2 or newer); a server without STARTTLS on 587 never gets the password. The Mailbox page's test email goes to
  the mailbox's own address only, 5 in 10 minutes. The inbox is read with EXAMINE and BODY.PEEK only (nothing is marked
  as read, moved or deleted), only new mail, and mail about no lead is never stored. Inbox mail is written by strangers:
  stored escaped, shown as text, never followed, and passed to Claude as data.
