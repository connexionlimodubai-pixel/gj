# The free open-source stack around OpenBerry

OpenBerry is the system of record: registration board, signals, scoring, outreach queue and the MCP server.
Claude can also use other free MCP servers in the same chat, so it can browse, search, read LinkedIn and push to a CRM.
This page lists the GitHub projects we checked (October 2026), what each one adds, and how to plug it in.

> Licences and commands were taken from each project's README/LICENSE. Always re-check the README before installing:
> these projects move fast.

## Recommended setup

| Need | Project | Licence | Why |
|---|---|---|---|
| **Claude ↔ your leads** | **OpenBerry** (this repo) | MIT | Registration board, signals, scoring, outreach drafts, 24 MCP tools |
| Read web pages | [modelcontextprotocol/servers — fetch](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) | MIT | Zero-setup URL → markdown for company research |
| Browse like a human (JS sites, careers pages) | [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | Persistent browser profile, works with logged-in sites |
| Web search without paid APIs | [searxng/searxng](https://github.com/searxng/searxng) + [ihor-sokoliuk/mcp-searxng](https://github.com/ihor-sokoliuk/mcp-searxng) | AGPL-3.0 / MIT | Self-hosted metasearch; `site:linkedin.com/in` queries |
| LinkedIn research (optional, **ToS risk**) | [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server) | Apache-2.0 | Profiles, company posts, people search through your own logged-in session |
| Local LLM for the dashboard | [ollama/ollama](https://github.com/ollama/ollama) | MIT | "Local AI (Ollama)" writer when drafting on a lead page; no API costs |

The Claude Desktop config with OpenBerry plus fetch and Playwright is in
[`claude_desktop_config.example.json`](../claude_desktop_config.example.json).

## MCP servers you can add next to OpenBerry

### Fetch (official reference server)
```json
{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}
```
It obeys robots.txt by default and can't render JavaScript-heavy pages (use Playwright for those).

### Playwright MCP (Microsoft)
```bash
claude mcp add playwright npx @playwright/mcp@latest          # Claude Code
```
```json
{"mcpServers": {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}
```
Useful flags: `--user-data-dir <path>` (keep logins), `--isolated`, `--extension` (drive your running Chrome).

### LinkedIn MCP server (stickerdaniel), optional
This server gives Claude `get_person_profile`, `search_people`, `get_company_posts`, `search_posts` and more, all through your own browser session.
OpenBerry's `get_prospecting_plan` tool tells Claude what to search, and `add_leads` stores the people it finds.
```bash
uvx mcp-server-linkedin@latest --login      # one-time login in a real browser window
```
```json
{"mcpServers": {"mcp-server-linkedin": {"command": "uvx", "args": ["mcp-server-linkedin@latest"],
                 "env": {"UV_HTTP_TIMEOUT": "300"}}}}
```
⚠️ **Read this before using it.** LinkedIn's User Agreement (§8.2) prohibits automated access, and accounts can be restricted. The project's README says the same.
Use it slowly, for research only, and keep sending manual. A lighter read-only alternative is
[eliasbiondo/linkedin-mcp-server](https://github.com/eliasbiondo/linkedin-mcp-server) (MIT, no messaging tools).

### SearXNG + mcp-searxng (self-hosted search)
```bash
mkdir -p searxng/core-config && cd searxng
curl -fsSL -O https://raw.githubusercontent.com/searxng/searxng/master/container/docker-compose.yml \
     -O https://raw.githubusercontent.com/searxng/searxng/master/container/.env.example
cp .env.example .env && docker compose up -d
```
```json
{"mcpServers": {"searxng": {"command": "npx", "args": ["-y", "mcp-searxng"],
                "env": {"SEARXNG_URL": "http://localhost:8888"}}}}
```
Enable JSON output in the SearXNG settings. Keep volumes modest, because upstream engines rate-limit heavy use.

### Firecrawl / Crawl4AI (deep crawling)
- [firecrawl/firecrawl-mcp-server](https://github.com/firecrawl/firecrawl-mcp-server) (MIT). To stay free, point `FIRECRAWL_API_URL` at a
  self-hosted [Firecrawl](https://github.com/firecrawl/firecrawl) (AGPL-3.0).
- [unclecode/crawl4ai](https://github.com/unclecode/crawl4ai) (Apache-2.0). The Docker image has a built-in MCP endpoint:
  `docker run -d -p 11235:11235 --shm-size=1g unclecode/crawl4ai:latest`, then
  `claude mcp add --transport sse c4ai-sse http://localhost:11235/mcp/sse`.

## CRM, email and automation

| Project | Licence | How it fits |
|---|---|---|
| [twentyhq/twenty](https://github.com/twentyhq/twenty) | AGPL-3.0 (+ enterprise files) | Open-source CRM with a built-in MCP server. Claude can copy hot leads from OpenBerry into Twenty. |
| [espocrm/espocrm](https://github.com/espocrm/espocrm) + [ext-mcp](https://github.com/espocrm/ext-mcp) | AGPL-3.0 | Lighter PHP CRM. Its MCP extension needs protocol 2026-07-28. |
| [reacherhq/check-if-email-exists](https://github.com/reacherhq/check-if-email-exists) | MIT | Self-hosted email verification: `docker run -p 8080:8080 reacherhq/backend:latest`. It needs outbound port 25. |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | Sustainable Use (fair-code) | Automations around OpenBerry's JSON API (`/api`), e.g. push hot leads to Slack or a CRM. |
| [activepieces/activepieces](https://github.com/activepieces/activepieces) | MIT (CE) | A fully open-source alternative to n8n, with a built-in MCP server. |
| [knadh/listmonk](https://github.com/knadh/listmonk) | AGPL-3.0 | Newsletters and nurture to opted-in contacts. It is not for cold email. |

## Other open-source "Gojiberry alternatives" we looked at

- [DigiHold/LinkedGrow](https://github.com/DigiHold/LinkedGrow) (AGPL-3.0): the closest feature clone. It runs real Chrome signed in to
  LinkedIn and needs a paid LLM key plus a proxy for each account.
- [vanshyadav1408/Omentir](https://github.com/vanshyadav1408/Omentir) (MIT): calls itself an "open source HeyReach & Gojiberry
  alternative". It depends on Firebase, Gemini and Unipile, which is paid.
- [debpalash/OpenGTM](https://github.com/debpalash/OpenGTM) (AGPL-3.0): a self-hosted Clay alternative (enrichment tables) with an MCP server.

OpenBerry's design choice is different: **no LinkedIn automation built in, no paid dependencies.** Claude is the agent,
public data sources supply the signals, and a human presses "send". The one exception is opt-in: with
[AI agent sending](AI_AGENT_SENDING.md), your own agent in your own browser sends the approved LinkedIn messages.
