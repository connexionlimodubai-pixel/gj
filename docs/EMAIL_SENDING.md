# Email sending from your own mailbox

This optional feature lets OpenBerry email the emails **you approve**, from **your own mailbox**, over your mail
provider's normal SMTP server. It also reads the same mailbox (read-only) so that replies, opt-outs and bounces stop
the sequence by themselves.

It is **off** for every company until you turn it on, on the company's **Outreach** page. Claude and the JSON API can
turn it off or make it slower, never turn it on.

- [1. What it does, and the risk](#1-what-it-does-and-the-risk)
- [2. Connect your mailbox](#2-connect-your-mailbox) (Zoho Mail, Hostinger, Gmail)
- [3. A separate mailbox, SPF, DKIM, DMARC and small numbers](#3-a-separate-mailbox-spf-dkim-dmarc-and-small-numbers)
- [4. Turn it on](#4-turn-it-on)
- [5. Replies, opt-outs and bounces](#5-replies-opt-outs-and-bounces)
- [6. Auto-approve and fully automatic sending](#6-auto-approve-and-fully-automatic-sending)
- [7. When something goes wrong](#7-when-something-goes-wrong)
- [8. What OpenBerry deliberately doesn't do](#8-what-openberry-deliberately-doesnt-do)
- [9. The law in short](#9-the-law-in-short) (not legal advice)
- [Questions](#questions) and [sources](#sources)

## 1. What it does, and the risk

### How it works

1. You (or Claude) write email drafts in OpenBerry, as usual.
2. You read each one and click **Approve**. Only approved emails are sent.
3. While OpenBerry is open, it sends them one at a time, from your mailbox, exactly as you approved them, with your
   footer added below.
4. Every 5 minutes it reads your inbox (and junk folder). A reply stops the sequence for that person. "Unsubscribe",
   "stop" or "remove me" (also in Arabic) puts the address on your never-email list. A bounce marks the address as
   not working.

### The risk: read this before you turn it on

- **Your mail provider may not allow it.** Hostinger's rules don't allow emails to people who didn't opt in, and
  Hostinger can block a mailbox that sends them. Zoho Mail is made for one-to-one business email: bulk or burst
  sending isn't allowed. Gmail and Google Workspace have their own sender rules. A blocked mailbox can take your
  normal client email down with it.
- **Use a separate mailbox for outreach**, ideally on a separate domain. Never the mailbox your customers write to.
- **The law applies to business emails too** (see [9](#9-the-law-in-short)). Say who you are, give a real business
  address, and honour every opt-out.
- **Start small.** 5 to 10 emails a day for the first two weeks.

### What OpenBerry guarantees

These rules are checked by OpenBerry itself every time, whatever the dashboard, Claude, a script or a bug asks for.

| Rule | What it means |
|---|---|
| Off by default | Email sending is off for every company. Only the Outreach page turns it on. Claude and the JSON API may only turn it off, lower the daily limit, space emails further apart, narrow the sending hours or stop auto-approved emails being sent. |
| Mailbox connected and inbox checked | Nothing is sent unless your mailbox is connected and its inbox was read successfully in the last 30 minutes, so a reply or opt-out is seen first. |
| Approved emails only, exactly as approved | The subject and text as you approved them, plus your footer. Plain text only. If the text changes, it is a draft again. While an email is being sent, it can't be edited or deleted. After it is sent, its text can't change. |
| Auto-approved emails only with their own switch | Drafts approved by [auto-approve](AI_AGENT_SENDING.md#auto-approve-optional) are emailed only if you also tick **Also send emails that auto-approve approved** (off by default). |
| A real recipient | A valid address, never a no-reply or system address, never an address on your never-email list (opted out, bounced, or marked by you), never an address whose domain is on your never-contact list. If a lead's address changes, its approved emails go back to draft. |
| The right people | Never leads who replied, booked a meeting, were won, lost or disqualified, or are on your never-contact list. Never for a paused company. |
| One at a time | One email per person at a time, never the same step twice, and a follow-up only once its wait has passed (counting messages sent on any channel). |
| Limits and pace | At most your daily limit in any 24 hours (default **30**, at most **100**), at least **5 minutes** between two emails (you can choose more), only in your sending hours (default **Monday to Friday, 09:00 to 18:00**, on your computer's clock), and at most 100 emails a day from the mailbox for all companies together. |
| Never twice | Each email is claimed, then marked "being sent" just before your mail server receives it, then recorded as sent. If the connection breaks while the server is receiving it, OpenBerry can't know whether it went out: it shows **May have been sent** and never sends it again by itself. |
| Stops by itself | On a reply, an opt-out, a bounce, a refused login, a security problem, the provider refusing to send, or repeated failures. |
| No tracking | No open pixels, no link changes, no HTML. |

## 2. Connect your mailbox

Open **Mailbox** in the sidebar. Choose your provider: it fills in the servers. Then type your email address, your
name and your password, and click **Save and check inbox**.

- Your password is saved in OpenBerry's settings file in your data folder (`~/.openberry/.env`, or the folder in
  `OPENBERRY_HOME`), which only your user account can read. It is never shown again, never stored in the database,
  never given to Claude or the JSON API, and never written to a log.
- OpenBerry always connects encrypted: SMTP on port 465 (TLS) or 587 (STARTTLS), IMAP on port 993, with the server's
  certificate checked. Other ports are refused.
- **Send test email** sends a short email to your own address only (5 tests in 10 minutes), then checks the inbox.
- **Check inbox** signs in over IMAP and reads the inbox without changing anything.
- **Disconnect** removes the settings and the password, and turns email sending off for every company.

### Zoho Mail (recommended for low-volume outreach)

| Plan | Sending server (SMTP) | Reading server (IMAP) |
|---|---|---|
| Paid plan, your own domain | `smtppro.zoho.com`, port 465 (or 587) | `imappro.zoho.com`, port 993 |
| Personal or free plan | `smtp.zoho.com`, port 587 (or 465) | `imap.zoho.com`, port 993 |

- Your account's data centre changes the ending: `.eu` (Europe), `.in` (India), `.com.au` (Australia). Pick it under
  **Zoho data centre**. Zoho's IMAP settings page shows your exact servers; you can type them over the preset.
- Turn on IMAP in Zoho Mail: **Settings → Mail Accounts → IMAP Access**.
- Username: your full email address. With two-step sign-in, create an **app-specific password** in Zoho (My Account →
  Security → App passwords) and use it here.
- The From address must be your mailbox or one of its aliases.
- Zoho limits outgoing email to between 50 and 500 an hour, depending on your sending reputation, and doesn't allow
  bulk or burst sending. OpenBerry sends one email at a time, minutes apart. Zoho's free business plans may not include
  IMAP and SMTP access.

### Hostinger Email

- Sending: `smtp.hostinger.com`, port 465 (or 587 with STARTTLS). Reading: `imap.hostinger.com`, port 993.
- Username: your full email address. hPanel shows these under **Emails → Connect apps & devices**.
- **Hostinger forbids email to people who did not opt in**, and can block your mailbox for it. If you use Hostinger,
  keep it for your clients and use another mailbox, ideally on another domain, for outreach.

### Gmail and Google Workspace

- Sending: `smtp.gmail.com`, port 465. Reading: `imap.gmail.com`, port 993.
- Turn on two-step verification, then create an **app password** (Google Account → Security → App passwords). Your
  normal password won't work.
- Gmail keeps a copy of what you send by itself, so **Save a copy in Sent** is off for this preset.

Microsoft 365 and Outlook.com need a sign-in method OpenBerry doesn't have, so they aren't supported.

### What the errors mean

| You see | What to do |
|---|---|
| Your mailbox refused the username or password | Use the full email address as the username, and the mailbox's password or an app password. Sending stays paused until you save the settings again or click **Try again**. |
| The server's security certificate isn't valid | The server name is wrong (for example `mail.yourdomain.com` instead of `smtppro.zoho.com`). |
| Can't find the server | A typo in the server name, or no internet connection. |
| This server doesn't offer encryption on port 587 | Use port 465. OpenBerry never sends your password unencrypted. |
| Your mailbox won't send from this address | Use the mailbox's own address, or an alias of it, as the From address. |
| OpenBerry couldn't read your inbox | Check IMAP access in your mail settings. Nothing is sent until the inbox can be read. |

## 3. A separate mailbox, SPF, DKIM, DMARC and small numbers

- **A separate mailbox, ideally on a separate domain** (for example `acme-mail.com` next to `acme.com`). If it gets
  blocked or lands on a blocklist, your main domain and your client email are safe.
- **SPF, DKIM and DMARC** tell receiving servers your emails are really from you. Your mail provider shows the DNS
  records to add (Zoho: Admin Console → Domains; Hostinger: hPanel → Emails). Without them, many of your emails go to
  spam.
- **Warm up slowly, and keep it small.**

| When | Daily limit |
|---|---|
| A new mailbox or domain, the first two weeks | 5-10 |
| Then, with few bounces and no complaints | up to 30 (the default) |
| Our advice from one mailbox | at most 50 (OpenBerry allows 100) |

Provider limits (Hostinger's daily limits per mailbox, Zoho's hourly limits) are ceilings, not targets, and they can
change without notice.

## 4. Turn it on

On the company's **Outreach** page, open **Email sending (your mailbox)**:

1. Set the **daily limit**, the **minutes between emails** and the **sending hours** (your computer's clock).
2. Write your **footer**: who you are, your business address, and how to opt out with the word "unsubscribe". It is
   added under every email when it is sent, and shown under every email draft. The example footer has a
   `[your business address]` to fill in: OpenBerry refuses a footer with a placeholder left in it.
3. Read the warnings, tick **I've read this**, and click **Turn on**. It needs a connected mailbox and a good inbox
   check.

Then approve email drafts as usual. Each email shows what will happen: **Queued for your mailbox**, **Sending…**,
**Sent from your mailbox**, **May have been sent**, **Not sent**, **Bounced**, or why it waits ("another email to this
lead goes first", "the next message to this lead is not due before …"). **Send now** sends the next one at once (still
keeping every rule). Close OpenBerry and sending waits; a laptop that sleeps doesn't send.

## 5. Replies, opt-outs and bounces

OpenBerry reads your inbox and junk folder every 5 minutes while it is open. It never marks mail as read, moves or
deletes anything. It reads only new mail (never your history), and only messages about your leads: everything else
is ignored and not stored.

- **A reply** is logged on the lead's page. The lead moves to Replied, and their drafts and approved messages are
  skipped (emails and LinkedIn).
- **An opt-out** ("unsubscribe", "stop", "remove me", "don't email me", "إلغاء الاشتراك"...), or the email a mail
  app's Unsubscribe button sends: the address goes on your **never-email list**, for every company, and the lead is
  disqualified and tagged `unsubscribed`. A company lead also goes on the company's never-contact list. This happens
  at the next inbox check, within minutes.
- **A hard bounce** (the address doesn't exist): the address goes on the never-email list, and the lead is tagged
  `email-bounced`. Three hard bounces in 24 hours pause email sending for that company for 24 hours.
- **Auto-replies** (out of office) and **delayed delivery** notices are noted on the lead's page; the sequence goes on.

You can also click **Unsubscribed (never email again)** on a lead's page, or ask Claude to log a reply with
`opt_out=true`. The never-email list is on the **Mailbox** page. It stays when a lead is deleted, so a business found
again on Google Maps stays off it. Remove a bounced address once it is fixed; remove an opt-out only if the person
asked to hear from you again.

## 6. Auto-approve and fully automatic sending

[Auto-approve](AI_AGENT_SENDING.md#auto-approve-optional) approves drafts nobody approved, held or skipped once
their review window has passed. Those emails are **not** sent from your mailbox unless you also tick **Also send
emails that auto-approve approved** on the Email sending card. With both on, emails go out without anyone reading
them, including template drafts for new hot leads and drafts Claude writes. The card then says **Fully automatic**.
Turn it on only if you trust every draft.

## 7. When something goes wrong

| What happened | What OpenBerry does |
|---|---|
| Login refused, a certificate problem, an unknown server, a From address not allowed | Pauses the mailbox until you save its settings or click **Try again** on the Mailbox page. The email waits. |
| The provider refuses to send (spam, a limit, a suspended mailbox) | The email isn't sent. The mailbox pauses for 24 hours. Check your mailbox in the provider's webmail before you resume. |
| The server can't be reached | Tries again after 5, 15, then 60 minutes. |
| The address doesn't exist | The email isn't sent and the address goes on the never-email list. |
| The connection broke while the server was receiving the email | **May have been sent.** Check your mailbox's Sent folder, then choose **Mark sent** or **Send again** (it may then arrive twice). A reply or bounce that refers to it settles it by itself. |
| OpenBerry closed while sending | The same: before the server received anything, the email is simply tried again; during, it may have been sent. |
| A sent email couldn't be saved in OpenBerry's database | OpenBerry stops sending until you restart it, so nothing can go out twice. |
| 3 hard bounces, or 2 spam rejections, in 24 hours for a company | Email sending pauses for that company for 24 hours. Only you can resume it early, on the Outreach page. |

## 8. What OpenBerry deliberately doesn't do

- No tracking pixels, no link rewriting, no HTML templates, no attachments.
- No bulk newsletters. For marketing email to people who opted in, use a marketing service (Zoho Campaigns, for
  example).
- No rotating mailboxes or domains, no random "human-like" delays, no tricks to hide what it is doing.
- No sending while OpenBerry is closed, and nothing in the cloud. Only the dashboard (`openberry serve` or the
  desktop app) sends: never `openberry mcp`, never Claude, never the JSON API.

## 9. The law in short

This isn't legal advice. Rules differ by country; check the ones that apply to you and to the people you email.

- **UAE**: the Personal Data Protection Law (Federal Decree-Law 45 of 2021) needs a lawful basis for processing
  personal data and gives people the right to object to direct marketing. Many lawyers read it as requiring consent
  for email marketing.
- **EU and UK**: the GDPR and the UK's PECR. Business addresses of named people are personal data. Some countries
  require consent before any marketing email.
- **USA**: the CAN-SPAM Act. Business-to-business email isn't exempt. Identify yourself, give a valid postal address,
  use an honest subject, and honour opt-outs within 10 business days (OpenBerry does it within minutes).

A public contact address on a website or a map listing isn't consent. OpenBerry's footer, the List-Unsubscribe
header and the automatic opt-out handling help you follow these rules; they don't make an email lawful by themselves.

## Questions

**Which provider should I use?** For a few personal emails a day, a paid Zoho Mail mailbox on a separate domain is a
good fit. Keep Hostinger for your clients: its rules forbid email to people who didn't opt in.

**Does OpenBerry send from my computer?** Yes: it connects to your mail provider's server, like a mail app does. A
network that only reaches the internet through a proxy can't send.

**What does the email look like?** Plain text: your approved subject and text, a blank line, then your footer. The
From address and name are yours. Mail apps can show an Unsubscribe button: it emails your mailbox, and OpenBerry stops
at the next inbox check, within minutes.

**Can Claude send emails?** No. Claude writes drafts; you approve them. Claude can read the email status
(`get_email_status`), log a reply with `opt_out=true`, or turn email sending off. It can't turn it on, raise its
limits, change your footer or resume it.

## Sources

Checked in October 2026. Providers change their limits and rules without notice: check the live pages.

- Zoho Mail: [SMTP server configuration](https://www.zoho.com/mail/help/zoho-smtp.html) (smtppro.zoho.com for paid
  organizations, smtp.zoho.com for personal and free accounts, ports 465 and 587),
  [IMAP access](https://www.zoho.com/mail/help/imap-access.html) (imappro.zoho.com / imap.zoho.com, port 993,
  app-specific passwords), [limits and policies](https://www.zoho.com/mail/help/adminconsole/rates-and-limits.html)
  (50-500 external emails an hour depending on reputation; bulk or burst sending not permitted).
- Hostinger: [Can I send mass mail?](https://support.hostinger.com/en/articles/1583510-can-i-send-mass-mail)
  (mail to people who didn't opt in isn't allowed),
  [configuration details](https://support.hostinger.com/en/articles/1575756-how-to-get-configuration-details-for-hostinger-email)
  (imap.hostinger.com 993, smtp.hostinger.com 465 or 587),
  [parameters and limits](https://support.hostinger.com/en/articles/4625828-parameters-and-limits-of-hostinger-email).
- Google: [Sign in with app passwords](https://support.google.com/accounts/answer/185833),
  [email sender guidelines](https://support.google.com/a/answer/81126).
- FTC: [CAN-SPAM Act: A Compliance Guide for Business](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business).
- UAE PDPL: [Baker McKenzie: direct marketing in the UAE](https://resourcehub.bakermckenzie.com/en/resources/global-data-and-cyber-handbook/emea/uae/topics/cookies-online-tracking-and-direct-marketing).
- RFCs: 5322 (message format), 8314 (TLS for email submission and access), 2369 (List-Unsubscribe), 3464 (bounce
  reports), 3463 (status codes), 3834 (auto-replies), 6154 (special IMAP folders), 3501 (IMAP, EXAMINE, BODY.PEEK).
