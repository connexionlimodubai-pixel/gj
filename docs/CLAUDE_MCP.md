# Using OpenBerry from Claude (MCP)

OpenBerry ships an MCP server, so Claude can work on your pipeline as an AI SDR. It can:
- read a company's profile from the registration board
- run signal scans and plan prospecting
- add the people it finds with other tools
- qualify leads
- draft personal outreach
- write pipeline reports
- optionally, send approved LinkedIn messages from your own browser ([AI agent sending](AI_AGENT_SENDING.md), off by default)

Everything Claude saves shows up in the dashboard, and the dashboard's data is what Claude sees.

## 1. Connect

### Claude Desktop (local, stdio)
1. Install [uv](https://docs.astral.sh/uv/) and clone this repo.
2. Open *Settings → Developer → Edit Config* and paste this (adjust the path):
   ```json
   {
     "mcpServers": {
       "openberry": {
         "command": "uv",
         "args": ["--directory", "/ABSOLUTE/PATH/TO/gj", "run", "openberry", "mcp"]
       }
     }
   }
   ```
   [`claude_desktop_config.example.json`](../claude_desktop_config.example.json) also adds the free `fetch` and `playwright` servers.
3. Restart Claude Desktop. The tools appear under the 🔌 menu.
   Claude Desktop doesn't always see your shell's `PATH`: if the server doesn't start, put uv's full path (`which uv`)
   in `"command"`. The dashboard's **Connect Claude** page (`/help`) shows the exact command and config for your install.

The dashboard and Claude share the same database, `~/.openberry/openberry.db`. If you set `OPENBERRY_DB` (or `OPENBERRY_HOME`),
use an absolute path and set it in both places (for Claude, add `"env": {"OPENBERRY_DB": "/abs/path.db"}` to the config).

### Claude Code
This repo includes a project-scoped [`.mcp.json`](../.mcp.json). Run `claude` inside the repo folder and approve the `openberry` server. From anywhere else:
```bash
claude mcp add openberry -- uv --directory /ABSOLUTE/PATH/TO/gj run openberry mcp
```

### Docker / remote (Streamable HTTP)
The dashboard serves MCP at `/mcp`. With a token set (`OPENBERRY_API_TOKEN`):
```bash
claude mcp add --transport http openberry http://localhost:8000/mcp \
  --header "Authorization: Bearer $OPENBERRY_API_TOKEN"
```
Without a token (local mode) it only answers on localhost and the host of `OPENBERRY_BASE_URL`; add other host names or a
LAN IP to `OPENBERRY_ALLOWED_HOSTS`. You can also use stdio through the container: `docker exec -i openberry openberry mcp`.
A standalone HTTP server, with the same auth, is `openberry mcp --http --port 8001`.

## 2. Tools

OpenBerry gives Claude 24 tools.

| Tool | What Claude uses it for |
|---|---|
| `list_companies` | See registered companies with lead/hot counts |
| `get_company_profile` | Read the ICP, offer, signal setup, and which sources are configured |
| `register_company` / `update_company` | Onboard a company from a chat, or change its ICP, keywords or outreach style (`update_company` replaces lists and `signals.weights` whole), including your LinkedIn account type (`outreach.linkedin_account`: `free` or `premium`) when you tell Claude which one you have. Alert webhooks must be Slack or Discord incoming-webhook URLs. Claude can't turn AI agent sending on, raise its limit or lift its pause, and can't turn [auto-approve](AI_AGENT_SENDING.md#auto-approve-optional) on or shorten its window (it can turn either off) |
| `run_signal_scan` | Run the free collectors now (HN, job boards, news, RSS, GitHub, SEC, Reddit) |
| `get_prospecting_plan` | Get concrete LinkedIn/Google searches, competitor and influencer pages, lookalikes, and events to research |
| `list_leads` / `get_lead` | Browse leads by tier, status or score; see the score reasons, signals and messages |
| `add_leads` | Save people found with other tools (LinkedIn MCP, browser, search), with the signal that explains *why now*. People who turn hot trigger the company's alert |
| `add_signal` | Record a new intent signal on an existing lead |
| `update_lead` / `delete_lead` | Change status, notes, tags or profile fields |
| `assess_lead` | Give Claude's 0-100 fit/timing judgement and rationale (blended 30% into the score) |
| `get_outreach_context` | Everything needed to write one message: sender, tone, offer, lead, signals, thread. Without a channel and step it prepares the next message in the sequence. For a connection note, `limits` gives your LinkedIn account's note length (`max_chars`: 200 free, 300 Premium) and, on a free account, the 5 notes a month and how many were sent in the last 30 days |
| `save_outreach_message` | Store a draft. Enforces your LinkedIn account's connection-note limit (200 characters on a free account, 300 on Premium) and your banned words. **Never sends.** With auto-approve on, it says when the draft will be approved automatically (`auto_approves_at`), so Claude can tell you. A new version of a draft you held is held too |
| `list_outreach` / `update_message` | Review the queue, edit drafts, mark sent. Claude sets *approved* only when you approve that exact text; editing an approved message makes it a draft again. Messages show `auto_approved` and, with auto-approve on, each draft's `auto_approves_at` (or `auto_approve`: `held` or why not; `get_lead` too). `update_message` can hold a draft (`auto_hold=true`) so it is never approved automatically, and `status="draft"` on an approved message holds it; only you release a hold, in the dashboard |
| `log_reply` | Record the lead's reply. This stops the follow-up sequence. |
| `followups_due` | Leads waiting for their next follow-up, with the step and channel to use |
| `pipeline_report` | Numbers and suggestions for a weekly report |
| `export_leads_csv` | CSV for a CRM or an outreach tool |
| `get_send_queue` | [AI agent sending](AI_AGENT_SENDING.md) only. The approved LinkedIn messages your browser agent may send now, with the exact text and profile link. With auto-approve on, it first approves the drafts whose review window has passed (`auto_approved_now`, and `auto_approved` on each item). Empty, with the reason, while sending is off, paused or at the daily limit. It also gives the connection limits: `connect_sent_7d` of `weekly_connect_limit` (80), `connect_notes_30d` of `monthly_note_limit` (5 on a free account, `null` on Premium) and `connect_blocked_reason`. Past them, connection requests wait and LinkedIn messages still come |
| `confirm_message_sent` | The agent records each message right after sending it. OpenBerry checks every rule again (including the note length and the connection limits) and counts it toward the daily limit |
| `report_send_problem` | The kill switch. On any LinkedIn warning, check or limit, it pauses agent sending for 24 hours and puts the message back to approved |

Resources: `openberry://companies`, `openberry://company/{id}/profile`, `openberry://company/{id}/hot-leads`.
Prompts: `onboard_company`, `daily_lead_hunt`, `write_outreach`, `weekly_report`, `send_approved_messages` (AI agent sending).

Slack/Discord alerts and `auto_draft` drafts also cover leads Claude makes hot: they go out within one scheduler tick
(5 minutes by default) while `openberry serve` runs, or with the next signal scan.

## 3. Things to ask Claude

- *"Let's set up OpenBerry for my company. Read our website at example.com, then interview me and register us."*
- *"Run today's lead hunt for company 1: scan signals, follow the prospecting plan, add the best 20 people with why they're a fit."*
- *"Show the 10 hottest leads for company 1 and explain each in one line."*
- *"For each account lead of company 1 (companies that are hiring or just raised), find the decision-maker and add them."*
- *"Draft LinkedIn connection notes for every hot lead with no message yet, in our tone."* Claude keeps them within your
  LinkedIn account's limit: 200 characters on a free account, 300 on Premium.
- *"I have LinkedIn Premium: update company 1."* (sets `outreach.linkedin_account`; every company starts as free)
- *"Here's a reply from Omar: '…'. Log it and draft an answer that books a call."*
- With auto-approve on: *"Hold the draft to Omar, I want to check it myself."* (`update_message` with `auto_hold=true`)
- *"Write my weekly pipeline report for company 1."*
- With AI agent sending on, and Claude able to use your browser: *"Use the openberry tools: run the send_approved_messages
  prompt for company 1."* In Claude Code, `/mcp__openberry__send_approved_messages 1` does the same.

## 4. With a LinkedIn MCP server (optional, at your own risk)

Add [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server) next to OpenBerry (see
[OPEN_SOURCE_STACK.md](OPEN_SOURCE_STACK.md)). Then ask:

> *"Use the prospecting plan for company 1. With the LinkedIn tools, look at the latest posts of our competitors' pages and
> the influencers listed, find people who commented and match our ICP, and add them with `add_leads`
> (signal type `competitor_engagement` or `influencer_engagement`, with the post URL). Go slowly: at most 30 profiles."*

LinkedIn prohibits automated access and may restrict your account. Keep volumes low, and don't send messages through
the LinkedIn MCP server. OpenBerry itself never sends messages: to have an agent in your own browser send the ones you
approved, see the next section.

## 5. AI agent sending (optional, at your own risk)

Turn it on per company on the **Outreach** page. Claude, in your own browser (Claude in Chrome, or Playwright MCP in
extension mode), then sends only approved LinkedIn messages, exactly as approved:

1. `get_send_queue(company_id)` returns the approved `linkedin_connect` and `linkedin_dm` messages it may send now.
2. Claude sends each one from your browser, then calls `confirm_message_sent(message_id)`.
3. At any warning, verification, CAPTCHA, limit or anything unexpected, it calls `report_send_problem` and stops.
   Sending pauses for 24 hours, and the Outreach page shows the reason and a **Resume** button.

OpenBerry enforces the rules itself: off by default, approved messages only, LinkedIn only (never email), a rolling
24-hour limit (default 15), never leads who replied or are excluded, and never a step twice. Connection requests also
follow LinkedIn's limits: notes no longer than your account allows (200 characters free, 300 Premium), at most 80
connection requests in any 7 days, and on a free account a note on at most 5 in any 30 days, counting every
connection request recorded as sent, yours included. Past those, connection requests wait while LinkedIn messages
still go out, and the agent never sends a connection request without its approved note. Changing an approved
text, or the lead's LinkedIn profile, makes the message a draft again. Claude can turn agent
sending off or lower its limit when you ask, but can't turn it on, raise the limit or lift a pause. With
[auto-approve](AI_AGENT_SENDING.md#auto-approve-optional) also on (off by default), drafts you don't approve, hold or
skip are approved after the review window (an edit starts it again), so your agent may send them without anyone reading
them. Setup, risks and limits: [AI_AGENT_SENDING.md](AI_AGENT_SENDING.md).
