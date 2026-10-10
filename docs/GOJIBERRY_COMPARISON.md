# OpenBerry vs Gojiberry: feature map

Gojiberry (gojiberry.ai) is a paid AI SDR, about $99/month for its Pro plan per third-party copies of its pricing page.
It watches mostly LinkedIn for intent signals, scores people against your ICP, and runs AI-personalised LinkedIn campaigns.
This table shows how each piece is replicated for free in OpenBerry. "Claude" means Claude Desktop or Claude Code using OpenBerry's MCP tools.

| Gojiberry feature | OpenBerry | How |
|---|---|---|
| Onboarding from your website URL | ✅ | The registration board's "Auto-fill from website" button. Claude's `onboard_company` prompt can also interview you and read your site. |
| ICP: titles, industries, sizes, locations, keywords, exclusions | ✅ | Registration board step 3 (adds seniorities, a never-contact list, and company types that guide Claude's prospecting) |
| Signal agents running 24/7 | ✅ | A built-in scheduler scans each company every *N* hours. Claude runs `daily_lead_hunt` on demand. |
| Competitor engagement | ✅ / ⚠️ | Competitor mentions on Hacker News and Reddit (Reddit needs your own API app), plus issue authors and forkers on competitor GitHub repos. On LinkedIn: Claude + an optional LinkedIn MCP server (ToS risk) |
| Topic / keyword posts | ✅ | Hacker News, Reddit and RSS. LinkedIn post search through Claude + a LinkedIn MCP server |
| Hiring | ✅ | Greenhouse, Lever and Ashby public job boards, plus HN "Who is hiring" |
| Funding | ✅ | Google News, RSS (e.g. TechCrunch), SEC EDGAR Form D (US) |
| Job changes / new leaders | ✅ / ⚠️ | News ("appoints … as CEO"), SEC 8-K item 5.02 (US public companies), Claude re-checking profiles |
| Influencer / creator engagers | ⚠️ | Claude + a LinkedIn MCP server, using the influencer list from the registration board |
| Profile visitors / page followers | ⚠️ | No public API. Paste exports into Claude, which adds them with `add_leads` (type `profile_visit`). |
| Events | ✅ / ⚠️ | Event names on the registration board. Claude looks up speakers and attendees with fetch/Playwright. |
| Lookalikes of best customers | ✅ | Best customers on the registration board. `get_prospecting_plan` turns them into lookalike searches. |
| Local business lists | ➕ | Google Maps businesses with contact details from their own websites (your Google key, free tier) |
| AI lead scoring with "intent reasons" | ✅ | Transparent scoring: ICP fit + time-decayed intent + signal stacking + optional Claude score (`assess_lead`). Every reason is shown. |
| Account-level intent | ✅ | Hiring, funding and news create *account* leads. People at that company inherit their intent. |
| AI-personalised messages | ✅ | Claude (`get_outreach_context` → `save_outreach_message`), local Ollama, or a template |
| Sequences / follow-ups | ✅ | Follow-up schedule (e.g. day 3, day 7). The "Follow-ups due" queue stops when a reply is logged. |
| Review / Copilot mode | ✅ | Always on. `auto_draft` mode drafts a first message for every person lead that turns hot (in a scan, through Claude, the API, an import or an edit), but **nothing is sent without your approval**. |
| Unified inbox | ⚠️ | Log replies on the lead page or with `log_reply`. Claude drafts the answer from the full thread. |
| Automated LinkedIn sending | ⚠️ (opt-in, your own agent, at your own risk) | OpenBerry itself never sends. By default you copy each approved draft and send it yourself. If you turn on [AI agent sending](AI_AGENT_SENDING.md) for a company, an AI agent in your own logged-in browser (Claude in Chrome, or Playwright MCP's extension mode) sends the LinkedIn notes and messages you approved, exactly as approved. OpenBerry enforces the limits: 15 a day by default, never leads who replied or are excluded, and a 24-hour pause on any LinkedIn warning. It runs only while your agent runs, never sends email, and has no tricks to hide automation. LinkedIn's User Agreement forbids automation, so your account can be restricted. |
| Email waterfall enrichment (15+ providers) | ⚠️ | Not built in. Claude + fetch can find public emails, and [Reacher](https://github.com/reacherhq/check-if-email-exists) verifies them for free. |
| CRM sync (HubSpot, Pipedrive) | ✅ / ⚠️ | CSV export, the JSON API (`/api`) for n8n/Activepieces, and Twenty CRM's MCP server next to OpenBerry |
| Slack alerts | ✅ | Slack and Discord webhooks when a person lead turns hot (70+, or a higher threshold set per company), however it got there. Sent after each scan and, while `openberry serve` runs, within minutes. |
| Hosted MCP server | ✅ | `openberry mcp` (stdio) or `/mcp` (Streamable HTTP, bearer token) |
| Dashboard / analytics | ✅ | KPI tiles, signals per day, signal mix, hot leads, scan history, reply rate |
| Multiple brands / clients | ✅ | Each registered company has its own ICP, signals and pipeline. Public registration mode lets clients fill in the form themselves; their companies wait, paused, for your review. |

✅ = built in · ➕ = OpenBerry extra · ⚠️ = possible with Claude plus a companion tool, or partially · ❌ = intentionally not built

## Where the free version is weaker

- **LinkedIn coverage.** Gojiberry's core data is LinkedIn activity. OpenBerry only reaches it through Claude and an optional LinkedIn MCP server, at your own risk.
- **Sending.** Gojiberry runs your LinkedIn campaigns for you. OpenBerry's optional agent sending works only while your own agent runs in your browser,
  sends only what you approved, and stops at the first LinkedIn warning.
- **Contact data.** There is no paid people database or email waterfall. Account-level signals tell you *which company* to contact.
  Claude then finds *who* using public sources.
- **Claude costs.** The software is free, but Claude usage comes with your Claude plan. The scheduler, collectors, scoring and
  template drafts run without Claude.
