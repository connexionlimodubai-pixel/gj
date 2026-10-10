# 🍓 OpenBerry

**A free, self-hosted, open-source alternative to [Gojiberry](https://gojiberry.ai).** It finds warm B2B leads from
public intent signals, scores them against your ideal customer, and drafts personal outreach. Claude does the
thinking through MCP, and a web dashboard (with a registration board for your company details and requirements) keeps everything in one place.

![Dashboard](docs/screenshots/dashboard.png)

## Download the app

| Windows 10/11 | Mac (Apple silicon) | Linux |
|---|---|---|
| [**OpenBerry for Windows**](https://github.com/connexionlimodubai-pixel/gj/releases/latest/download/OpenBerry-windows-x64.zip) (39 MB) | [**OpenBerry for Mac**](https://github.com/connexionlimodubai-pixel/gj/releases/latest/download/OpenBerry-macos-arm64.zip) (39 MB) | [**OpenBerry for Linux**](https://github.com/connexionlimodubai-pixel/gj/releases/latest/download/OpenBerry-linux-x64.tar.gz) (43 MB) |
| Unzip (right-click → **Extract All**), then double-click **OpenBerry.exe**. If Windows says it protected your PC, click **More info → Run anyway**. | Unzip, drag **OpenBerry.app** to Applications, then right-click it → **Open** the first time. | Extract and run `./OpenBerry`: it opens in your browser. |

These links always download the newest version. Release notes and older versions are on the [Releases page](https://github.com/connexionlimodubai-pixel/gj/releases).
The app isn't code-signed, which is why Windows and macOS ask you to confirm once. Step-by-step help: [docs/DESKTOP.md](docs/DESKTOP.md).

## What it does

1. **Register your company** on the registration board: what you sell, who buys it (your ICP), which signals to watch,
   your requirements and your outreach style. It can auto-fill from your website.
2. **Collect intent signals** on a schedule from free public sources:
   - Hacker News, GitHub, Greenhouse/Lever/Ashby job boards, Google News, RSS and SEC EDGAR
   - Reddit, if you have API access
   - Google Maps businesses (hotels, event planners, law firms...) with the email and phone from their own websites,
     if you add a Google Maps API key
   - LinkedIn and anything else Claude can browse
3. **Score every lead** transparently: ICP fit + time-decayed intent + signal stacking, plus Claude's own judgement.
   Every point is explained ("Title matches 'Travel Manager'", "Hiring for a relevant role, 3d ago").
4. **Draft outreach** with Claude (or a free local Ollama model, or templates): LinkedIn notes, DMs, emails and follow-ups.
   **Nothing is sent before it is approved.** You review, copy, send and mark as sent, or, if you choose, let your own
   AI agent send the approved LinkedIn messages ([AI agent sending](#ai-agent-sending-optional-at-your-own-risk)).
   Approve drafts one at a time, or tick several on the Outreach page and approve them together. If you can't keep up,
   turn on **auto-approve** (off by default): drafts you don't approve, hold or skip are approved after a review window
   you choose (2 hours by default, counted from the last edit), and never for leads who replied or are on your
   never-contact list.
5. **Alert you** on Slack or Discord when a person lead turns hot, whether a scan, Claude, the API, a CSV import or an edit
   made it hot. Alerts go out after each scan and, while `openberry serve` runs, within a few minutes.
   Export to CSV or use the JSON API with n8n or Activepieces.

| Registration board | Register a company | Lead detail |
|---|---|---|
| ![Registration board](docs/screenshots/registration-board.png) | ![Register](docs/screenshots/register.png) | ![Lead](docs/screenshots/lead.png) |

See [docs/GOJIBERRY_COMPARISON.md](docs/GOJIBERRY_COMPARISON.md) for a feature-by-feature comparison.

## Desktop app (no terminal needed)

Download OpenBerry for **Windows**, **Mac** (Apple silicon) or **Linux** [above](#download-the-app) or from the
[Releases page](https://github.com/connexionlimodubai-pixel/gj/releases/latest), unzip it and double-click **OpenBerry**.
The dashboard opens in its own window, and your data stays on your computer. Claude Desktop connects to it
through the **Connect Claude** page in the app. You don't need Python or a terminal.

The app isn't code-signed yet, so Windows and macOS ask you to confirm the first time you open it.
[docs/DESKTOP.md](docs/DESKTOP.md) explains each step, how to update, and how to build and release the app.

## Quick start (5 minutes)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), a fast Python installer. Python 3.11+ is fetched automatically.

```bash
git clone https://github.com/connexionlimodubai-pixel/gj.git openberry && cd openberry
uv run openberry demo     # optional: a demo company with sample leads (all fictional)
uv run openberry serve    # → open http://127.0.0.1:8000
```

Click **Register company**, fill in the form, then **Run scan now** on your dashboard.
Data lives in `~/.openberry/openberry.db` (move it with `OPENBERRY_HOME` or `OPENBERRY_DB`).

### With Docker
```bash
cp .env.example .env      # set OPENBERRY_PASSWORD, OPENBERRY_SECRET_KEY and OPENBERRY_API_TOKEN if others can reach it
docker compose up -d      # → http://localhost:8000
```

## Connect Claude (the AI SDR)

Add OpenBerry to **Claude Desktop** (*Settings → Developer → Edit Config*):
```json
{
  "mcpServers": {
    "openberry": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/openberry", "run", "openberry", "mcp"]
    }
  }
}
```
Claude Desktop doesn't always see your shell's `PATH`: if it can't start `uv`, put uv's full path (`which uv`) in `"command"`.
The dashboard's **Connect Claude** page (`/help`) shows the exact command and config for your install, Docker and the
desktop app included (the app's config runs its bundled `openberry-cli mcp`).

For **Claude Code**: run `claude` inside this folder (the included `.mcp.json` registers the server), or run
`claude mcp add openberry -- uv --directory /ABSOLUTE/PATH/TO/openberry run openberry mcp`.

Then ask Claude things like:
- *"Set up OpenBerry for my company: read example.com, interview me, and register us."*
- *"Run today's lead hunt for company 1 and show me the 10 hottest leads with why."*
- *"Find the decision-makers at the companies that are hiring, and add them."*
- *"Draft LinkedIn connection notes for every hot lead that has no message yet."*

Claude gets 24 tools (scan, prospecting plan, add leads, assess, outreach context, save draft, log reply, pipeline report…),
plus prompts such as `daily_lead_hunt`. See [docs/CLAUDE_MCP.md](docs/CLAUDE_MCP.md).
To let Claude browse LinkedIn and the web too, add free open-source MCP servers next to OpenBerry
(Playwright, fetch, SearXNG, and optionally a LinkedIn MCP server at your own risk). See [docs/OPEN_SOURCE_STACK.md](docs/OPEN_SOURCE_STACK.md).

## AI agent sending (optional, at your own risk)

OpenBerry never opens LinkedIn or sends anything itself. If you turn on **AI agent sending** for a company (Outreach page,
off by default), an AI agent running in your own browser, logged in to your LinkedIn, can send the approved LinkedIn
messages. Claude in Chrome and Playwright MCP's extension mode both work. OpenBerry enforces the rules itself:
- approved messages only, exactly as approved, and LinkedIn only (never email)
- a daily limit (default 15 in any 24 hours), and LinkedIn's connection limits: notes of at most 200 characters on a
  free LinkedIn account (300 on Premium), a note on at most 5 connection requests a month on a free account, and at
  most 80 connection requests in any 7 days
- never leads who replied or are on your never-contact list
- a 24-hour pause, with a Resume button, as soon as the agent reports a LinkedIn warning, check or limit

If you also turn on **auto-approve**, your agent may send drafts you didn't read in time: they are approved once their
review window has passed. Hold the ones you want to check first. Auto-approve never approves the first LinkedIn message
after a connection request (did they accept?), a step already sent, or a draft with a banned word or an unfilled
placeholder. Claude and the JSON API can turn agent sending and auto-approve off, never on.

There are no tricks to hide automation. LinkedIn's User Agreement forbids automated messaging, so your account can be
restricted. Read [docs/AI_AGENT_SENDING.md](docs/AI_AGENT_SENDING.md) before you turn it on.

## Configuration

Everything is optional for local use. Put settings in `.env` (read from the current folder and `~/.openberry/.env`)
or in the environment. See [`.env.example`](.env.example) for the other settings.

| Variable | Purpose |
|---|---|
| `OPENBERRY_PASSWORD`, `OPENBERRY_SECRET_KEY` | Dashboard login. **Required** if anyone but you can reach the server. |
| `OPENBERRY_API_TOKEN` | Bearer token for `/api` and the HTTP MCP endpoint `/mcp` |
| `OPENBERRY_BASE_URL` | Public URL of the dashboard: links in Claude replies, an allowed host name, and Secure login cookies when it starts with `https://` |
| `OPENBERRY_ALLOWED_HOSTS` | Extra host names to answer to in local mode (no password), e.g. a LAN IP so `/mcp` works there |
| `OPENBERRY_PUBLIC_REGISTRATION=true` | Let clients fill in the registration form themselves (agency intake). Their companies wait, paused, for your review |
| `OPENBERRY_CONTACT_EMAIL` | Contact e-mail for SEC EDGAR's required User-Agent |
| `OPENBERRY_GOOGLE_PLACES_KEY` | Google Maps API key for the Google Maps businesses source. Desktop users can paste it on the **API keys** page instead |
| `OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT` | Most Google Maps searches a month (default 900; Google's free tier is 1,000) |
| `GITHUB_TOKEN` | Higher GitHub limits; stargazers of repos you admin |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME` | Optional Reddit source (see its terms) |
| `OPENBERRY_OLLAMA_URL`, `OPENBERRY_OLLAMA_MODEL` | Free local LLM: the "Local AI (Ollama)" writer in a lead's *Draft a message* card |
| `OPENBERRY_SCHEDULER=false` | Turn off automatic scans and the background hot-lead alerts |
| `OPENBERRY_HOME` | Data folder for `openberry.db` and the fallback `.env` (default `~/.openberry`). Environment only, not `.env` |
| `OPENBERRY_ENV_FILE` | Read settings only from this file instead of `./.env` and `~/.openberry/.env`. Environment only |

## Commands

```bash
openberry serve [--host 0.0.0.0 --port 8000]   # dashboard + /mcp + background scheduler
openberry desktop [--no-window]                # the dashboard in its own window (uv sync --extra desktop), else the browser
openberry mcp                                  # MCP over stdio (what Claude Desktop runs)
openberry mcp --http --port 8001               # standalone MCP over HTTP (bearer token)
openberry scan [--company 1] [--source hackernews]
openberry demo | rescore | init-db
```
(Prefix with `uv run` if you haven't installed it with `pip install .`.)
If you change `--host` or `--port`, set `OPENBERRY_BASE_URL` to the URL you open in the browser: Claude's links use it,
and without a password the server only answers to that host, `localhost` and IP addresses.

## How it works

```
Claude Desktop / Code ──MCP──▶ OpenBerry ◀── dashboard (registration board, leads, outreach)
        │                          │
        └─ optional MCP servers    ├─ collectors: HN · GitHub · job boards · News/RSS · SEC · Reddit · Google Maps
           (Playwright, fetch,     ├─ scoring: ICP fit + intent decay + signal stacking + Claude score
            LinkedIn, SearXNG)     └─ SQLite (~/.openberry/openberry.db)
```
More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/SIGNALS.md](docs/SIGNALS.md).

## Responsible use

- OpenBerry never sends messages or automates LinkedIn itself. LinkedIn's User Agreement prohibits automation. If you add a
  LinkedIn MCP server or turn on [AI agent sending](docs/AI_AGENT_SENDING.md), that is your decision and your account's risk.
- Each data source has terms. They are summarised in [docs/SIGNALS.md](docs/SIGNALS.md). Google News RSS is for personal,
  non-commercial use, and Reddit requires an agreement for commercial use.
- Prospect data is personal data. Comply with GDPR, the UAE PDPL, CAN-SPAM and similar laws: contact people with relevant, honest messages and
  honour opt-outs.
- Google Maps: OpenBerry keeps only each business's Place ID, as Google's terms require. Contact details come from the
  business's own website. Read the [Google Maps Platform Terms](https://cloud.google.com/maps-platform/terms) before you use it.
- Bulk email: these are business contact addresses. Email providers block mailboxes that send to people who didn't opt
  in (Hostinger's rules say so), and anti-spam laws apply. Send few, personal, relevant emails and honour opt-outs.
  OpenBerry never sends email itself.

## Development

```bash
uv sync --extra dev
uv run pytest            # ~1,180 tests, no network needed
```

MIT licensed. Not affiliated with Gojiberry.
