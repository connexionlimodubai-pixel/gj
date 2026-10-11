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
| Mailbox connected and inbox checked | Nothing is sent unless your mailbox is connected and its inbox was read successfully, to the last new message, in the last 30 minutes, so a reply or opt-out is seen first. A check time in the future (the computer's clock was set back) doesn't count: the inbox is checked again at once. |
| Approved emails only, exactly as approved | The subject and text as you approved them, plus your footer. Plain text only. If the text changes, it is a draft again. While an email is being sent, it can't be edited or deleted. After it is sent, its text can't change. |
| Auto-approved emails only with their own switch | Drafts approved by [auto-approve](AI_AGENT_SENDING.md#auto-approve-optional) are emailed only if you also tick **Also send emails that auto-approve approved** (off by default). |
| A real recipient | A valid address, never a no-reply or system address, never your own mailbox (use **Send test email**), never an address on your never-email list (opted out, bounced, or marked by you), never an address whose domain is on your never-contact list. If a lead's address changes, its approved emails go back to draft, also one being sent at that moment. |
| The right people | Never leads who replied, booked a meeting, were won, lost or disqualified, opted out (tagged `unsubscribed`, whatever address they get later), or are on your never-contact list. Never for a paused company. A change you make while an email is being sent (the lead marked Lost, the never-contact list, a switch) stops it before your mail server receives it. |
| One address, one recipient | An address found through several leads (two hotel branches with one reservations@, a person and their company's info@, even in two companies) is one recipient: it gets one email at a time, never the same step twice, and a reply through one lead stops the emails of all of them, also once that lead has another address or was deleted. |
| One at a time, in order | One email per person at a time, never the same step twice, never a follow-up before the earlier step was sent or skipped, and a follow-up only once its wait has passed (counting messages sent on any channel, and emails that may have been sent). |
| Limits and pace | At most your daily limit in any 24 hours (default **30**, at most **100**), at least **5 minutes** between two emails (you can choose more), only in your sending hours (default **Monday to Friday, 09:00 to 18:00**, on the clock of the computer OpenBerry runs on: the card names its time zone), and at most 100 emails a day from the mailbox for all companies together. |
| Never twice | Each email is claimed, then marked "being sent" just before your mail server receives it, then recorded as sent once the server accepted it (an email the server refuses after receiving it is not counted as sent). If the connection breaks while the server is receiving it, OpenBerry can't know whether it went out: it shows **May have been sent** and never sends it again by itself. |
| Deleting never erases what was sent | Deleting a lead, an email or a whole company keeps a record of every email sent or possibly sent (the address, the step, when, and whether they replied). It still counts for never twice, one email at a time, the daily limits and replies, in every company. A lead, an email or a company can't be deleted while one of its emails is being sent. |
| Stops by itself | On a reply, an opt-out, a bounce, a refused login, a security problem, the provider refusing to send, or repeated failures. |
| No tracking | No open pixels, no link changes, no HTML. |

## 2. Connect your mailbox

Open **Mailbox** in the sidebar. Choose your provider: it fills in the servers. Then type your email address, your
name and your password, and click **Save and check inbox**. Saving checks reading only: then click **Send test email**
to check sending, before you turn sending on.

- Your password is saved in OpenBerry's settings file in your data folder (`~/.openberry/.env`, or the folder in
  `OPENBERRY_HOME`), which only your user account can read. It is never shown again, never stored in the database,
  never given to Claude or the JSON API, and never written to a log. If you change a server or the username, type the
  password again: OpenBerry never sends a saved password to a server it wasn't typed for.
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
- Username: your full email address. With two-step sign-in, create an **app-specific password**: sign in at
  accounts.zoho.com, open **Security → App Passwords**, click **Generate New Password**, name it "OpenBerry", and paste
  the password it shows here (Zoho shows it once).
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
| Your mailbox refused the username or password | Use the full email address as the username, and the mailbox's password or an app password. Sending and the automatic inbox checks stay paused until you save the settings again, or click **Try again** or **Check inbox**: a mail provider can lock an account that keeps trying a wrong password. |
| The server's security certificate isn't valid | The server name is wrong (for example `mail.yourdomain.com` instead of `smtppro.zoho.com`). |
| Can't find the server | A typo in the server name. If it worked before, the internet connection dropped for a moment: OpenBerry tries again by itself after 5, 15, then 60 minutes. |
| This server doesn't offer encryption on port 587 | Use port 465. OpenBerry never sends your password unencrypted. |
| Your mailbox won't send from this address | Use the mailbox's own address, or an alias of it, as the From address. Sending waits until you save the settings again; the inbox checks go on, so replies and opt-outs are still read. Changing the From address keeps where the inbox check stopped and any pause from your provider. |
| OpenBerry couldn't read your inbox | Check IMAP access in your mail settings. Nothing is sent until the inbox can be read. |

## 3. A separate mailbox, SPF, DKIM, DMARC and small numbers

- **A separate mailbox, ideally on a separate domain** (for example `acme-mail.com` next to `acme.com`). If it gets
  blocked or lands on a blocklist, your main domain and your client email are safe.
- **SPF, DKIM and DMARC** tell receiving servers your emails are really from you. Without them, many of your emails go
  to spam. They are DNS records of the domain you send from, added where its DNS is managed: often your domain
  registrar, or Hostinger (hPanel → Domains → DNS / Nameservers) if the domain is there.
  1. **SPF**: your mail provider gives one TXT record. For Zoho (Admin Console → Domains → your domain → Email
     Configuration → SPF) it looks like `v=spf1 include:zoho.com ~all` (`zoho.eu`, `zoho.in`... for other data
     centres). A domain has only one SPF record: if one exists, add the `include:` to it.
  2. **DKIM**: in Zoho, Admin Console → Domains → your domain → Email Configuration → DKIM → Add. Zoho shows a
     selector and a long TXT value: add that TXT record, then click **Verify** in Zoho.
  3. **DMARC**: start with a TXT record named `_dmarc` and the value `v=DMARC1; p=none; rua=mailto:you@yourdomain.com`.
     It changes nothing for delivery yet and sends you reports; move to `p=quarantine` once SPF and DKIM pass.
  4. Wait for the records to take effect (up to a day), then send a test email to a Gmail address and check
     **Show original**: SPF, DKIM and DMARC should say PASS.
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

1. Set the **daily limit**, the **minutes between emails** and the **sending hours**. They follow the clock of the
   computer OpenBerry runs on, and the card names its time zone. On a server or in Docker (UTC unless you change it),
   set `TZ=Asia/Dubai` (your zone) in OpenBerry's `.env` file and restart it.
2. Write your **footer**: who you are, your business address, and how to opt out with the word "unsubscribe". It is
   added under every email when it is sent, and shown under every email draft. The example footer has a
   `[your business address]` to fill in: OpenBerry refuses a footer with a placeholder left in it.
3. Read the warnings, tick **I've read this**, and click **Turn on**. It needs a connected mailbox and a good inbox
   check.

Then approve email drafts as usual. Each email shows what will happen: **Queued for your mailbox**, **Sending…**,
**Sent from your mailbox**, **May have been sent**, **Not sent**, **Bounced**, or why it waits ("another email to this
lead goes first", "the next message to this lead is not due before …"). **Send now** sends the next one at once (still
keeping every rule); it is offered only when an email can go out now, not outside your sending hours or during the
wait between two emails. Close OpenBerry and sending waits; a laptop that sleeps doesn't send. Closing it while an
email is being sent waits for your mail server's answer first.

## 5. Replies, opt-outs and bounces

OpenBerry reads your inbox and junk folder every 5 minutes while it is open. It never marks mail as read, moves or
deletes anything. It reads only new mail (never your history), and only messages about your leads: everything else
is ignored and not stored. After a long time closed, it may need a few checks to read everything that arrived;
nothing is sent until it has. New reading details (another IMAP server for the same mailbox) start from the last
check, not from now.

Mail about a lead is found by the email it answers (In-Reply-To, References), the lead's address, or, when someone
else at the lead's company writes without answering an email (an assistant: "Lina is travelling, please remove her
from your list"), the company's own domain, never a free-mail one like gmail.com. The lead's page then says who wrote.
When it can't tell which of the people you emailed there it is about, it goes on each of their pages and none of them
gets another email. Only the one emailed last moves to Replied, and it counts as one reply in your numbers and on
the Replies tab.
A reply, an out-of-office or a bounce to an email that **may have been sent** settles it: it was sent, as if you had
clicked **Mark sent**.

- **A reply** is logged on the lead's page. The lead moves to Replied, and their drafts and approved messages are
  skipped (emails and LinkedIn).
- **An opt-out** that can't mean anything else, a short reply that just says "unsubscribe", "STOP", "remove me",
  "please take me off your list", "don't email me again" (or "إلغاء الاشتراك", "لا ترسل لي"...), or the email a mail
  app's Unsubscribe button sends: the address goes on your **never-email list**, for every company, and the lead is
  tagged `unsubscribed` (never emailed again, whatever address it gets; Claude and the JSON API can't remove that tag,
  only you can, on the lead's form) and disqualified. A lead in a meeting or won keeps its status, and the company's
  never-contact list is left to you. This happens at the next inbox check, within minutes.
- **A reply that only looks like an opt-out** ("Who gave you my email? Remove me from your list and send the rates to
  my office", "Stop, we booked with another company"): words in an email can be misread, so OpenBerry stores it as a
  reply (the lead's emails stop) and flags it **Looks like an opt-out** on the lead's page, the Outreach page and the
  dashboard. You decide: **Never email again** does the full opt-out above; **Not an opt-out** clears the flag. Claude
  sees the flag too (`get_lead`, `get_email_status`). Booking replies ("stop at the mall", "remove me from tomorrow's
  pickup list", "please don't send the car before 9") are plain replies, never opt-outs.
- **Newsletters** from a lead's address (a hotel's campaign from its info@) are noted, never read as a reply or an
  opt-out. Your own emails coming back to your mailbox are ignored.
- **A hard bounce** (the address doesn't exist): the address goes on the never-email list, and the lead is tagged
  `email-bounced`. Three hard bounces in 24 hours pause email sending for that company for 24 hours.
- **Auto-replies** (out of office) and **delayed delivery** notices are noted on the lead's page; the sequence goes on.

You can also click **Unsubscribed (never email again)** on a lead's page (a company lead then also goes on the
company's never-contact list), or ask Claude to log a reply with `opt_out=true`. The never-email list is on the **Mailbox** page. It stays when a lead is deleted, so a business found
again on Google Maps stays off it. Remove a bounced address once it is fixed; remove an opt-out only if the person
asked to hear from you again (their lead stays tagged `unsubscribed` until you remove the tag on its page).

## 6. Auto-approve and fully automatic sending

[Auto-approve](AI_AGENT_SENDING.md#auto-approve-optional) approves drafts nobody approved, held or skipped once
their review window has passed. Those emails are **not** sent from your mailbox unless you also tick **Also send
emails that auto-approve approved** on the Email sending card. With both on, emails go out without anyone reading
them, including template drafts for new hot leads and drafts Claude writes. The card then says **Fully automatic**.
Turn it on only if you trust every draft.

## 7. When something goes wrong

| What happened | What OpenBerry does |
|---|---|
| Login refused (sending or reading) | Pauses sending and the automatic inbox checks until you save the mailbox's settings or click **Try again** or **Check inbox** on the Mailbox page: a provider can lock an account that keeps trying a wrong password. The email waits. |
| A certificate problem, a server without encryption, a From address not allowed | Pauses sending until you save the settings or click **Try again**. The inbox checks go on, so replies and opt-outs are still read. The email waits. |
| A temporary sign-in problem on the inbox ("[UNAVAILABLE] Temporary authentication failure") | Nothing is paused: the inbox is read again at the next check, and nothing is sent until it is. |
| A reply couldn't be saved (OpenBerry's database was busy) | The inbox check counts as failed, so nothing is sent, and the message is read again at the next check. |
| The provider refuses to send (spam, a limit, a suspended mailbox), also after it received the email | The email isn't sent and isn't counted as sent. The mailbox pauses for 24 hours, also when you save its settings or change the From address. Check your mailbox in the provider's webmail before you click **Try again**. |
| The server can't be reached, or its name can't be found for a moment (Wi-Fi dropped) | Tries again after 5, 15, then 60 minutes. |
| The address doesn't exist (also Microsoft 365's "Recipient address rejected", a domain that doesn't exist, or a group that takes no outside mail) | The email isn't sent and the address goes on the never-email list. Fix the address on the lead's page, or skip the email. |
| The connection broke while the server was receiving the email | **May have been sent.** Check your mailbox's Sent folder, then choose **Mark sent** or **Send again** (it may then arrive twice). With **Save a copy** on (every preset but Gmail), OpenBerry puts the copy in Sent itself and couldn't for this email, so the Sent folder can't tell: choose **Mark sent** if unsure. A reply or bounce settles it by itself, also after **Send again** (even if the re-send fails). The email can't be edited meanwhile. After **Send again** it can, and then a reply to the first copy only stops it: the lead never got the new text. |
| OpenBerry closed while sending | The same: before the server received anything, the email is simply tried again; during, it may have been sent. |
| A sent email couldn't be saved in OpenBerry's database | OpenBerry stops sending until you restart it, so nothing can go out twice. |
| The computer's clock went back (dual-boot, a wrong time zone fixed) | A last inbox check or scan stamped in the future counts as old: the inbox is checked at once. The minutes between two emails count from the last email as recorded, so after a big jump back the next email may wait until the clock catches up. |
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
