# Changelog

What changed in each OpenBerry release, newest first. When a version is released, its section here becomes the
release notes on the [Releases page](https://github.com/connexionlimodubai-pixel/gj/releases).

## 0.2.0

This version adds email sending from your own mailbox, auto-approve, a Google Maps company finder and backups. It
removes the demo. It also changes how your data is stored (database version 3 to 6), so read the upgrade steps first.

### Upgrading from 0.1.x

1. **Close OpenBerry and quit Claude Desktop from the tray** (right-click Claude's icon next to the clock, **Quit**;
   on a Mac, **Claude → Quit Claude**). Closing Claude's window is not enough: it keeps the old `openberry-cli` running.
2. **Back up.** Copy the whole `.openberry` folder in your home folder, with every file in it (`openberry.db`,
   `openberry.db-wal` and `openberry.db-shm` if they are there, and `.env`). 0.1.x has no Download backup button.
3. **Replace the app** with the new one and open it. The upgrade is automatic. OpenBerry first saves a copy of your
   data in `.openberry/backups` (named `openberry-pre-v3-…`), then upgrades it. Then open Claude Desktop again.
4. **There is no downgrade.** Once 0.2.0 has upgraded your data, don't open it with 0.1.x. To go back, put the copy
   from step 1 back while everything is closed.
5. **If you loaded the demo**, open its company and click **Delete sample company** in the banner at the top. The
   next page shows what goes and what stays, and nothing is removed until you confirm. Its fictional people are
   never contacted in the meantime.
6. **Docker:** the container no longer starts without a password. Set `OPENBERRY_PASSWORD` (12 characters or more)
   in `.env`, in single quotes if it has a `$` in it, or `OPENBERRY_ALLOW_NO_PASSWORD=true` if only you can reach it.
   Then update with `git pull && docker compose up -d --build`.

Step-by-step help: [docs/DESKTOP.md](https://github.com/connexionlimodubai-pixel/gj/blob/main/docs/DESKTOP.md#upgrading-from-01x).

### New

- **Email sending from your own mailbox** (off by default). Connect Zoho Mail, Hostinger, Gmail or any SMTP and IMAP
  mailbox on the new **Mailbox** page, then turn on **Email sending** for a company on its Outreach page. OpenBerry
  then emails the emails you approved, exactly as approved, with your footer, within a daily limit and your sending
  hours. It reads your inbox: a reply stops the sequence, an opt-out puts the address on a never-email list, and a
  bounce marks the address as not working. A reply that only looks like an opt-out is flagged **Looks like an
  opt-out**, and you decide.
- **Auto-approve** (off by default). Drafts you don't approve, hold or skip are approved after a review window you
  choose (2 hours by default, counted from the last edit). Never for leads who replied or are on your never-contact
  list. Hold a draft to approve it yourself.
- **Google Maps company finder.** Lists the businesses from your Google Maps searches, with the email and phone from
  their own websites. It needs your Google Maps API key, which you paste on the new **API keys** page. OpenBerry keeps
  only Google's Place ID, and stops at 900 searches a month by default (Google's free tier is 1,000).
- **Backups.** A new **Your data** page with **Download backup**. OpenBerry also saves a copy by itself every day
  (7 days and 4 weeks are kept), before an upgrade, before Delete company and before a restore. New commands:
  `openberry backup` and `openberry restore FILE`. A restore turns email sending and AI agent sending off.
- **Keep your place.** The Outreach tabs, Approve, Mark sent, bulk approve and the forms on the lead and profile
  pages reload the page where you were, not at the top.
- **Clear error pages** when the disk is full, the data file is damaged or the database is busy, and a health check
  (`/healthz`) that tests the database.
- **Full CSV export:** every lead (no 5,000 limit), with the kind, the created and updated dates and Claude's
  rationale.
- **Claude:** a read-only `get_email_status` tool (25 tools in all). Claude and the JSON API can turn email sending
  and auto-approve off, never on.

### Changed

- **The demo is gone** (`openberry demo` and **Load demo data**). Register your company, or ask Claude to interview
  you. A sample company left by an old demo shows a banner with a guided removal (also `openberry remove-sample`). It
  is no longer scanned while you leave it as it was, and its fictional people are never contacted.
- **Still no login on your own computer.** The desktop app and `openberry serve` on 127.0.0.1 work as before. On an
  address other computers can reach (Docker included), `openberry serve` refuses to start without
  `OPENBERRY_PASSWORD` (12 characters or more), unless you set `OPENBERRY_ALLOW_NO_PASSWORD=true`. It warns about a
  missing or short secret key or API token, and about an `http://` address on a network. `openberry mcp --http` on
  such an address refuses to start without `OPENBERRY_API_TOKEN`, even with `OPENBERRY_ALLOW_NO_PASSWORD=true`.
- 20 wrong API tokens in 10 minutes from one address get "429 Too Many Requests". `OPENBERRY_TRUSTED_PROXIES` gives
  each visitor their own login limit behind a reverse proxy. The data folder, its `.env` file, the database and the
  backups can be read by your user account only (Linux, macOS and Docker), also when an older version made them.
  More security headers, and no lead searches in the logs.
- Data saved by a newer OpenBerry is refused ("This data was saved by a newer OpenBerry") instead of damaged.
- Deleting a lead, a message or a company keeps the record of the emails sent, so nobody is emailed twice.

### Behind the scenes

- The Docker image installs exactly the packages in `uv.lock`, from base images pinned by digest, and runs read-only
  with no extra privileges.
- The tests run on Windows, macOS and Linux. A release is published only once it holds all three downloads, and its
  download links are checked.
- Every download holds `LICENSE.txt` and `THIRD_PARTY_NOTICES.txt`. GNU Readline is no longer in the Linux download.
- Database version 6 (it was 3 in 0.1.1).

## 0.1.1

- Bulk approve and skip on the Outreach Drafts tab.
- Visible confirmations when you save.
- The download names stay the same for every version, so the README's links always download the newest.

## 0.1.0

- The first release: the dashboard with the registration board, Claude's MCP server, free signal sources, lead
  scoring and outreach drafts.
- The desktop app for Windows, macOS and Linux.
- AI agent sending (off by default): an AI agent in your own browser sends the LinkedIn messages you approved.
