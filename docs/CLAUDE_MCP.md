# Using OpenBerry from Claude (MCP)

OpenBerry ships an MCP server, so Claude can work on your pipeline as an AI SDR. It can:
- read a company's profile from the registration board
- run signal scans and plan prospecting
- add the people it finds with other tools
- qualify leads
- draft personal outreach
- write pipeline reports

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

| Tool | What Claude uses it for |
|---|---|
| `list_companies` | See registered companies with lead/hot counts |
| `get_company_profile` | Read the ICP, offer, signal setup, and which sources are configured |
| `register_company` / `update_company` | Onboard a company from a chat, or change its ICP, keywords or outreach style (`update_company` replaces lists and `signals.weights` whole). Alert webhooks must be Slack or Discord incoming-webhook URLs |
| `run_signal_scan` | Run the free collectors now (HN, job boards, news, RSS, GitHub, SEC, Reddit) |
| `get_prospecting_plan` | Get concrete LinkedIn/Google searches, competitor and influencer pages, lookalikes, and events to research |
| `list_leads` / `get_lead` | Browse leads by tier, status or score; see the score reasons, signals and messages |
| `add_leads` | Save people found with other tools (LinkedIn MCP, browser, search), with the signal that explains *why now*. People who turn hot trigger the company's alert |
| `add_signal` | Record a new intent signal on an existing lead |
| `update_lead` / `delete_lead` | Change status, notes, tags or profile fields |
| `assess_lead` | Give Claude's 0-100 fit/timing judgement and rationale (blended 30% into the score) |
| `get_outreach_context` | Everything needed to write one message: sender, tone, offer, lead, signals, thread. Without a channel and step it prepares the next message in the sequence |
| `save_outreach_message` | Store a draft. Enforces the LinkedIn 300-character limit and your banned words. **Never sends.** |
| `list_outreach` / `update_message` | Review the queue, edit drafts, mark sent |
| `log_reply` | Record the lead's reply. This stops the follow-up sequence. |
| `followups_due` | Leads waiting for their next follow-up, with the step and channel to use |
| `pipeline_report` | Numbers and suggestions for a weekly report |
| `export_leads_csv` | CSV for a CRM or an outreach tool |

Resources: `openberry://companies`, `openberry://company/{id}/profile`, `openberry://company/{id}/hot-leads`.
Prompts: `onboard_company`, `daily_lead_hunt`, `write_outreach`, `weekly_report`.

Slack/Discord alerts and `auto_draft` drafts also cover leads Claude makes hot: they go out within one scheduler tick
(5 minutes by default) while `openberry serve` runs, or with the next signal scan.

## 3. Things to ask Claude

- *"Let's set up OpenBerry for my company. Read our website at example.com, then interview me and register us."*
- *"Run today's lead hunt for company 1: scan signals, follow the prospecting plan, add the best 20 people with why they're a fit."*
- *"Show the 10 hottest leads for company 1 and explain each in one line."*
- *"For each account lead of company 1 (companies that are hiring or just raised), find the decision-maker and add them."*
- *"Draft LinkedIn connection notes for every hot lead with no message yet. Keep them under 300 characters and in our tone."*
- *"Here's a reply from Omar: '…'. Log it and draft an answer that books a call."*
- *"Write my weekly pipeline report for company 1."*

## 4. With a LinkedIn MCP server (optional, at your own risk)

Add [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server) next to OpenBerry (see
[OPEN_SOURCE_STACK.md](OPEN_SOURCE_STACK.md)). Then ask:

> *"Use the prospecting plan for company 1. With the LinkedIn tools, look at the latest posts of our competitors' pages and
> the influencers listed, find people who commented and match our ICP, and add them with `add_leads`
> (signal type `competitor_engagement` or `influencer_engagement`, with the post URL). Go slowly: at most 30 profiles."*

LinkedIn prohibits automated access and may restrict your account. Keep volumes low and do all sending manually.
OpenBerry never sends messages.
