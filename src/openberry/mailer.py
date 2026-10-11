"""Email from the user's own mailbox: SMTP to send approved emails, IMAP to read replies, opt-outs and bounces.

No SQL here (repo.py decides what may be sent and records what happened) and the standard library only. Only the
dashboard process uses it (services.send_next_email and services.check_inbox, run by scheduler.email_loop and the
dashboard's buttons): `openberry mcp` never sends or reads mail.

Safety:
- Always encrypted: SMTP on port 465 (TLS from the start) or 587 (STARTTLS; refused when the server doesn't offer
  it, before the password is sent), IMAP on 993. Certificates and host names are always checked, TLS 1.2 or newer;
  there is no setting to turn that off.
- Sending goes step by step (EHLO, login, MAIL FROM, RCPT TO, then on_data() just before DATA), so the caller records
  that the server is about to receive the email: a connection lost after that is "unknown" and never retried
  automatically. Nothing is sent twice.
- IMAP is read-only: EXAMINE and BODY.PEEK only, so mail is never marked as read, moved, flagged or deleted. The only
  write is the optional copy of a sent email in the Sent folder (APPEND).
- Plain text only: no HTML, no tracking pixels, no link changes. The body is the approved text plus the footer.
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import imaplib
import re
import secrets
import smtplib
import socket
import ssl
import time
import unicodedata
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.headerregistry import Address
from functools import lru_cache
from email.message import EmailMessage, Message
from email.utils import format_datetime, parseaddr
from html.parser import HTMLParser
from typing import Any

SMTP_PORTS = (465, 587)
IMAP_PORT = 993
TIMEOUT = 30                     # seconds for each network step
EHLO_NAME = "[127.0.0.1]"        # as Thunderbird does: the computer's own name stays out of the email's headers
SERVER_TEXT_MAX = 300            # the server's words kept in an error message
INBOUND_TEXT_MAX = 5000          # the new text of a reply that is stored
INBOUND_BYTES_MAX = 256 * 1024   # the most of one inbox message that is read
INBOX_FETCH_MAX = 200            # inbox messages read in one check (per folder); more wait for the next check
FETCH_BATCH = 50

# Addresses that never get a cold email: nobody reads them, or they are the mail system's own.
NO_REPLY = re.compile(r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|abuse|bounces?|"
                      r"hostmaster|devnull|null)(?:[-_.+].*)?$", re.I)
_ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
                      r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")


def valid_address(address: str) -> bool:
    """A plain ASCII email address (no display name, no SMTPUTF8 in this version)."""
    address = (address or "").strip()
    return len(address) <= 254 and len(address.partition("@")[0]) <= 64 and bool(_ADDRESS.fullmatch(address))


def find_addresses(text: str, limit: int = 20) -> tuple[str, ...]:
    """Email addresses in a text, lower case, at most `limit`. Word by word, so a long line costs linear time."""
    found: list[str] = []
    for word in (text or "").split():
        if "@" in word and len(word) <= 320:
            match = _ADDRESS.search(word)
            if match and match.group(0).lower() not in found:
                found.append(match.group(0).lower())
                if len(found) >= limit:
                    break
    return tuple(found)


def no_reply_address(address: str) -> bool:
    return bool(NO_REPLY.match((address or "").strip().partition("@")[0]))


# Mail services anyone can sign up to: a sender there is no colleague of a lead with the same domain.
FREE_MAIL_DOMAINS = frozenset((
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.net",
    "mail.com", "zoho.com", "zohomail.com", "yandex.com", "yandex.ru", "mail.ru", "emirates.net.ae", "eim.ae",
    "du.ae", "qq.com", "163.com", "rediffmail.com",
))


def company_domain(address: str) -> str:
    """The address's domain when it is a company's own (not a free mail service), else ""."""
    domain = domain_of(address)
    return "" if not domain or domain in FREE_MAIL_DOMAINS else domain


def domain_of(address: str) -> str:
    return (address or "").strip().rpartition("@")[2].lower()


# --------------------------------------------------------------------------------------
# The mailbox and one email to send
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mailbox:
    """The connection details of the user's mailbox (from the Mailbox page's settings). Never logged or shown."""

    smtp_host: str = ""
    smtp_port: int = 465
    username: str = ""
    password: str = field(default="", repr=False)
    from_email: str = ""
    from_name: str = ""
    imap_host: str = ""
    imap_port: int = IMAP_PORT
    imap_username: str = ""
    imap_password: str = field(default="", repr=False)
    save_sent: bool = True
    provider: str = ""

    @classmethod
    def from_settings(cls, settings: Any) -> Mailbox | None:
        """The mailbox, or None while it isn't fully set up (Settings.mailbox_address)."""
        if not settings.mailbox_address():
            return None
        return cls(smtp_host=settings.smtp_host, smtp_port=settings.smtp_port, username=settings.smtp_username,
                   password=settings.smtp_password, from_email=(settings.smtp_from_email or settings.smtp_username),
                   from_name=settings.smtp_from_name, imap_host=settings.imap_host, imap_port=settings.imap_port,
                   imap_username=settings.imap_username, imap_password=settings.imap_password,
                   save_sent=settings.imap_save_sent, provider=settings.mail_provider)

    @property
    def address(self) -> str:
        return self.from_email.strip().lower()

    @property
    def key(self) -> str:
        """The account it signs in with: what the mailbox's state is kept under (Settings.mailbox_key)."""
        return self.username.strip().lower()

    @property
    def reader(self) -> tuple[str, str]:
        """The IMAP username and password: the SMTP ones unless the user gave others."""
        return (self.imap_username or self.username, self.imap_password or self.password)

    @property
    def reader_key(self) -> str:
        """The mailbox that is read (its IMAP server and username): where the inbox check stopped is kept under it."""
        return f"{self.imap_host.strip().lower()}|{self.reader[0].strip().lower()}"


@dataclass(frozen=True)
class EmailJob:
    """One claimed email (repo.claim_next_email): a snapshot, so the sending thread never reads the database."""

    message_id: int             # OpenBerry's message
    claim: str                  # the claim token: only this worker may mark it or record it
    header_id: str              # its Message-ID header, kept across attempts
    from_email: str
    from_name: str
    to_email: str
    to_name: str
    subject: str
    body: str
    footer: str
    references: tuple[str, ...] = ()  # Message-IDs of the earlier emails OpenBerry sent this lead, oldest first

    @property
    def unsubscribe_token(self) -> str:
        return self.header_id.strip("<>").partition("@")[0]


def new_header_id(from_email: str) -> str:
    """A Message-ID on the sender's own domain: <32 random hex characters@domain>."""
    return f"<{secrets.token_hex(16)}@{domain_of(from_email) or 'openberry.invalid'}>"


def full_body(body: str, footer: str) -> str:
    """The text that is sent: the approved body exactly as approved, a blank line, then the footer."""
    return f"{body}\n\n{footer}" if footer else body


def clean_name(name: str) -> str:
    """A display name for a header: lead data is written by strangers, so control characters go, whitespace
    collapses and it is at most 78 characters."""
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in name or "")
    return " ".join(text.split())[:78].strip()


def subject_breaks(subject: str) -> bool:
    """The subject has a line break of any kind (\\n, \\r, U+2028, NEL, VT, FF...): the email library refuses it in a
    header, so such an email is held back before it is claimed."""
    return len(f"{subject}x".splitlines()) > 1


# How an email is written on the wire: SMTP's CRLF, and header lines up to 998 characters (RFC 5322), so a header
# that can't be folded (List-Unsubscribe's <mailto:...>) is never turned into =?utf-8?q?...?= encoded words, which
# mail apps don't read as an address. The body keeps the 76-character lines of its quoted-printable encoding.
WIRE_POLICY = email.policy.SMTP.clone(max_line_length=998)


def build_email(job: EmailJob, now: datetime | None = None) -> EmailMessage:
    """The email exactly as it goes out: plain text, UTF-8, quoted-printable, CRLF line ends on the wire (serialize
    it with WIRE_POLICY).

    The subject is never changed: one with a line break is refused (ValueError), not "fixed".
    """
    if subject_breaks(job.subject):
        raise ValueError("the subject has a line break")
    if not job.subject.strip():
        raise ValueError("the email has no subject")
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = Address(display_name=clean_name(job.from_name), addr_spec=job.from_email)
    to_name = clean_name(job.to_name)
    msg["To"] = Address(display_name=to_name, addr_spec=job.to_email) if to_name else job.to_email
    msg["Subject"] = job.subject
    msg["Date"] = format_datetime((now or datetime.now(timezone.utc)).astimezone(timezone.utc))
    msg["Message-ID"] = job.header_id
    if job.references:
        msg["In-Reply-To"] = job.references[-1]
        msg["References"] = " ".join(job.references)
    # RFC 2369: the recipient's mail app can offer "Unsubscribe", which emails the mailbox OpenBerry reads. No
    # List-Unsubscribe-Post: RFC 8058's one-click needs an HTTPS address, which a local app doesn't have.
    msg["List-Unsubscribe"] = f"<mailto:{job.from_email}?subject=unsubscribe%20{job.unsubscribe_token}>"
    msg.set_content(full_body(job.body, job.footer), subtype="plain", charset="utf-8", cte="quoted-printable")
    return msg


# --------------------------------------------------------------------------------------
# Outcomes and errors in plain words
# --------------------------------------------------------------------------------------

# Outcome.kind -> what repo.record_email_failure does (docs/EMAIL_SENDING.md, "When something goes wrong").
SENT, ABORTED = "sent", "aborted"
AUTH, CONFIG, RETRY = "auth", "config", "retry"                   # the mailbox: paused, the email waits
BOUNCE, PROVIDER_BLOCK, FAILED = "bounce", "provider_block", "failed"  # this email: not sent
UNKNOWN = "unknown"                                                 # the connection broke during DATA


@dataclass(frozen=True)
class Outcome:
    kind: str
    text: str = ""         # what the user sees, in plain words (never the password)
    code: int = 0          # the server's reply code, when there was one
    status: str = ""       # the enhanced status code (5.1.1), when there was one
    data: bytes = field(default=b"", repr=False)  # what was sent (kind "sent")

    @property
    def ok(self) -> bool:
        return self.kind == SENT


class MailError(Exception):
    """An inbox (IMAP) problem, in plain words: kind is AUTH, CONFIG or RETRY."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text


def server_text(value: Any) -> str:
    """A server's reply as safe, short text: decoded, control characters removed, at most SERVER_TEXT_MAX."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in str(value or ""))
    text = " ".join(text.split())
    return text if len(text) <= SERVER_TEXT_MAX else text[:SERVER_TEXT_MAX - 1] + "…"


_STATUS = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
# The mail provider refusing to send for the mailbox (its limits, its spam filter, a suspended account), not a
# problem with one email or one address.
_BLOCKED_WORDS = re.compile(r"\b(?:spam|blocked|blacklist(?:ed)?|block ?list(?:ed)?|deny ?list(?:ed)?|suspend(?:ed)?|"
                            r"(?:sending|send|daily|hourly|rate|outgoing|message) ?limits?|"
                            r"limits? (?:exceeded|reached)|sending quota|too many|abuse|unusual|reputation|frozen|"
                            r"disabled)\b", re.I)
_NO_SUCH_USER = re.compile(r"user unknown|unknown user|does not exist|doesn't exist|no such (?:user|mailbox|recipient)|"
                           r"mailbox (?:not found|unavailable)|invalid (?:recipient|mailbox|address)|"
                           r"(?:recipient|user|account) (?:not found|unknown)|address (?:not found|unknown)|not a valid|"
                           r"(?:mailbox|account|user|recipient) (?:is |has been )?(?:disabled|inactive|deactivated)|"
                           r"unrouteable address|no mailbox here|(?:doesn't|does not) have an? [\w.-]+ account|"
                           r"account (?:has )?expired", re.I)
# A bounce report blaming the sender rather than the address (a policy rejection, 5.7.x as a rule).
_POLICY_WORDS = re.compile(r"\b(?:spam|blocked|blacklist(?:ed)?|block ?list(?:ed)?|deny ?list(?:ed)?|reputation|"
                           r"policy|abuse)\b", re.I)


def enhanced_status(text: str) -> str:
    match = _STATUS.search(text or "")
    return ".".join(match.groups()) if match else ""


def _hard_bounce_status(status: str) -> bool:
    return status.startswith("5.1.") or status == "5.2.1"


def _reply(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        code, resp = next(iter(exc.recipients.values()), (0, b""))
        return int(code), server_text(resp)
    return int(getattr(exc, "smtp_code", 0) or 0), server_text(getattr(exc, "smtp_error", "") or exc)


RETRY_TEXT = "Couldn't reach your mail server ({detail}). OpenBerry tries again in a few minutes."
FROM_REFUSED_TEXT = ("Your mailbox won't send from {from_email} ({said}). Use the mailbox's own address, or an alias "
                     "of it, as the From address.")
# The server refusing the From address at MAIL FROM: a setting to fix, not the provider blocking the mailbox.
_SENDER_REFUSED = re.compile(r"sender address rejected|not owned by|not allowed to (?:send|relay)|"
                             r"relay(?:ing)? (?:denied|disallowed|not permitted)", re.I)
UNKNOWN_TEXT = ("The connection broke while your mail server was receiving this email ({detail}). It may have gone "
                "out: check your mailbox's Sent folder, then choose Mark sent or Send again.")


def classify(exc: BaseException, phase: str, host: str = "", from_email: str = "") -> Outcome:
    """What a failed send means, by the step it failed at (connect, starttls, login, mail, rcpt, data).

    Before DATA nothing was handed over, so network problems are retried. During DATA a broken connection is
    "unknown": the server may have accepted the email, so it is never sent again automatically. A reply from the
    server is always a definite answer, also after DATA.
    """
    detail = server_text(exc) or type(exc).__name__
    if phase == "build":  # the email itself can't be written (a subject with a line break, an odd address)
        return Outcome(FAILED, f"This email can't be sent as it is: {detail}.")
    # Before ValueError: ssl's errors are ValueErrors too.
    if isinstance(exc, ssl.SSLCertVerificationError):
        return Outcome(CONFIG, f"The server's security certificate isn't valid for {host}. Check the server name, "
                               "for example smtppro.zoho.com or smtp.hostinger.com.")
    if isinstance(exc, ssl.SSLError):
        return Outcome(CONFIG, f"A secure connection to {host} failed ({detail}). Check the server name and port.")
    if isinstance(exc, ValueError):  # a port OpenBerry doesn't use (check_port): the settings need fixing
        return Outcome(CONFIG, f"Your mailbox settings can't be used: {detail}.")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        code, words = _reply(exc)
        return Outcome(AUTH, f"Your mailbox refused the username or password ({words}). Use the full email address "
                             "as the username and the mailbox's password, or an app password if the account has "
                             "two-step sign-in.", code, enhanced_status(words))
    if isinstance(exc, NoEncryption):
        return Outcome(CONFIG, str(exc))
    if isinstance(exc, socket.gaierror):  # no internet (a Wi-Fi drop) as often as a typo: tried again, not paused
        return Outcome(RETRY, f"Can't find the server {host}. Check the name and your internet connection. "
                              "OpenBerry tries again in a few minutes.")
    if isinstance(exc, smtplib.SMTPNotSupportedError) or (
            isinstance(exc, smtplib.SMTPException) and "authentication method" in detail.lower()):
        return Outcome(CONFIG, f"Your mail server doesn't accept OpenBerry's sign-in ({detail}).")
    if isinstance(exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused)):
        code, words = _reply(exc)
        status = enhanced_status(words)
        said = f"{code} {words}".strip()
        if 400 <= code < 500 or (code < 400 and phase != "data"):  # a temporary refusal: try again later
            return Outcome(RETRY, RETRY_TEXT.format(detail=said), code, status)
        no_such_user = phase == "rcpt" and (address_refused(status, words) or not status.startswith("5.7.") and (
            _hard_bounce_status(status) or bool(_NO_SUCH_USER.search(words))))
        if no_such_user:
            return Outcome(BOUNCE, f"The address doesn't exist (the server said: {said}).", code, status)
        if phase == "mail" and _SENDER_REFUSED.search(words):  # Postfix's "553 5.7.1 ... not owned by user"
            return Outcome(CONFIG, FROM_REFUSED_TEXT.format(from_email=from_email, said=said), code, status)
        if status.startswith("5.7.") or (not status.startswith("5.2.") and _BLOCKED_WORDS.search(words)):
            return Outcome(PROVIDER_BLOCK, f"Your mail provider refused to send it: {said}. Check your mailbox in "
                                           "the provider's webmail before you resume.", code, status)
        if phase == "mail":
            return Outcome(CONFIG, FROM_REFUSED_TEXT.format(from_email=from_email, said=said), code, status)
        if phase in ("connect", "starttls", "login"):
            return Outcome(CONFIG, f"Your mail server refused the connection ({said}). Check the server name, the "
                                   "port and your mailbox's settings.", code, status)
        return Outcome(FAILED, f"Your mail server refused this email: {said}", code, status)
    if isinstance(exc, (OSError, smtplib.SMTPException, EOFError)):  # refused, reset, timeout, disconnected
        if phase == "data":
            return Outcome(UNKNOWN, UNKNOWN_TEXT.format(detail=detail))
        return Outcome(RETRY, RETRY_TEXT.format(detail=detail))
    return Outcome(UNKNOWN if phase == "data" else RETRY,
                   (UNKNOWN_TEXT if phase == "data" else RETRY_TEXT).format(detail=detail))


class NoEncryption(smtplib.SMTPException):
    """The server on port 587 doesn't offer STARTTLS: the password would travel unencrypted, so nothing is sent."""


# --------------------------------------------------------------------------------------
# SMTP
# --------------------------------------------------------------------------------------


def tls_context() -> ssl.SSLContext:
    """Certificates and host names checked, TLS 1.2 or newer. certifi's certificates are added when it is installed
    (it comes with httpx): packaged apps and python.org's macOS builds may lack the system's."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with suppress(ImportError, OSError):
        import certifi

        ctx.load_verify_locations(certifi.where())
    return ctx


def check_port(port: int, allowed: Iterable[int]) -> None:
    if port not in tuple(allowed):
        raise ValueError(f"port {port} isn't used: only {' or '.join(str(p) for p in allowed)}")


def smtp_connect(mailbox: Mailbox) -> smtplib.SMTP:
    """An encrypted connection to the SMTP server: TLS from the start on 465, STARTTLS on 587 (or nothing)."""
    check_port(mailbox.smtp_port, SMTP_PORTS)
    ctx = tls_context()
    if mailbox.smtp_port == 465:
        conn: smtplib.SMTP = smtplib.SMTP_SSL(mailbox.smtp_host, 465, local_hostname=EHLO_NAME, timeout=TIMEOUT,
                                              context=ctx)
        conn.ehlo()
        return conn
    conn = smtplib.SMTP(mailbox.smtp_host, 587, local_hostname=EHLO_NAME, timeout=TIMEOUT)
    try:
        conn.ehlo()
        if not conn.has_extn("starttls"):
            raise NoEncryption("This server doesn't offer encryption on port 587. Use port 465.")
        conn.starttls(context=ctx)
        conn.ehlo()
    except BaseException:
        with suppress(Exception):
            conn.close()
        raise
    return conn


def _smtp_login(conn: smtplib.SMTP, username: str, password: str) -> None:
    try:
        conn.login(username, password)
    except UnicodeEncodeError:  # smtplib sends the password as ASCII: send AUTH PLAIN in UTF-8 instead
        token = base64.b64encode(f"\0{username}\0{password}".encode()).decode("ascii")
        code, resp = conn.docmd("AUTH", f"PLAIN {token}")
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, resp) from None


def send(job: EmailJob, mailbox: Mailbox, on_data: Callable[[], bool], now: datetime | None = None) -> Outcome:
    """Send one email, step by step. on_data() runs just before DATA: when it returns False (the email was taken
    out of the queue meanwhile, or sending was turned off) the server gets RSET instead, and nothing is sent.

    Never raises for a mail problem: the Outcome says what happened. MAIL FROM is the From address, so bounces come
    back to the mailbox OpenBerry reads.
    """
    phase = "build"
    conn: smtplib.SMTP | None = None
    try:
        data = build_email(job, now).as_bytes(policy=WIRE_POLICY)
        phase = "connect"
        conn = smtp_connect(mailbox)
        phase = "login"
        _smtp_login(conn, mailbox.username, mailbox.password)
        phase = "mail"
        code, resp = conn.mail(job.from_email)
        if code != 250:
            raise smtplib.SMTPSenderRefused(code, resp, job.from_email)
        phase = "rcpt"
        code, resp = conn.rcpt(job.to_email)
        if code not in (250, 251):
            raise smtplib.SMTPRecipientsRefused({job.to_email: (code, resp)})
        if not on_data():
            with suppress(Exception):
                conn.rset()
            return Outcome(ABORTED, "It was taken out of the queue just before sending.")
        phase = "data"
        # smtplib raises only when the server refuses to start DATA (no 354). Its final answer, once it has the whole
        # email, is returned: anything but 250 there means it refused the email (a spam filter, a policy, a limit).
        code, resp = conn.data(data)
        if code != 250:
            raise smtplib.SMTPDataError(code, resp)
        return Outcome(SENT, "", 250, data=data)
    except Exception as exc:
        return classify(exc, phase, mailbox.smtp_host, job.from_email)
    finally:
        if conn is not None:
            try:
                conn.quit()
            except Exception:
                with suppress(Exception):
                    conn.close()


TEST_SUBJECT = "OpenBerry test email"
TEST_BODY = ("This is a test email from OpenBerry. Your mailbox can send email.\n\n"
             "OpenBerry sent it to your own address only. You can delete it.")


def send_test(mailbox: Mailbox, now: datetime | None = None) -> Outcome:
    """Send a short test email to the mailbox's own From address (never to anyone else)."""
    job = EmailJob(message_id=0, claim="", header_id=new_header_id(mailbox.from_email), from_email=mailbox.from_email,
                   from_name=mailbox.from_name, to_email=mailbox.from_email, to_name=mailbox.from_name,
                   subject=TEST_SUBJECT, body=TEST_BODY, footer="")
    return send(job, mailbox, on_data=lambda: True, now=now)


# --------------------------------------------------------------------------------------
# IMAP (read-only)
# --------------------------------------------------------------------------------------

_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$', re.I)
JUNK_NAMES = ("Junk", "Spam", "INBOX.Junk", "INBOX.Spam", "INBOX.spam", "Junk E-mail", "Junk Email", "Bulk Mail")
SENT_NAMES = ("Sent", "INBOX.Sent", "Sent Items", "Sent Messages", "Sent Mail", "INBOX.Sent Items")


def imap_quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(day: date) -> str:
    """dd-Mon-yyyy in English whatever the computer's language (strftime's %b follows the locale)."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == '"':
        return re.sub(r"\\(.)", r"\1", name[1:-1])
    return name


def _imap_error(exc: BaseException, host: str) -> MailError:
    text = server_text(exc.args[0] if isinstance(exc, imaplib.IMAP4.error) and exc.args else exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return MailError(CONFIG, f"The server's security certificate isn't valid for {host}. Check the server name.")
    if isinstance(exc, ssl.SSLError):
        return MailError(CONFIG, f"A secure connection to {host} failed ({text}). Check the server name.")
    if isinstance(exc, socket.gaierror):
        return MailError(CONFIG, f"Can't find the server {host}. Check the name and your internet connection.")
    if isinstance(exc, ValueError):  # a port other than 993 (check_port)
        return MailError(CONFIG, f"Your mailbox settings can't be used for reading mail: {text}.")
    if isinstance(exc, imaplib.IMAP4.error):
        # RFC 5530: [UNAVAILABLE] is temporary (Dovecot's "Temporary authentication failure"): tried again later, never
        # a pause that waits for the user.
        if (re.search(r"\[UNAVAILABLE\]|temporar|try again", text, re.I)
                and not re.search(r"AUTHENTICATIONFAILED|AUTHORIZATIONFAILED", text, re.I)):
            return MailError(RETRY, f"Your mail server couldn't sign you in just now ({text}). OpenBerry tries again "
                                    "in a few minutes.")
        if re.search(r"AUTHENTICATIONFAILED|AUTHORIZATIONFAILED|LOGIN|credential|password|authenticat", text, re.I):
            return MailError(AUTH, f"Your mailbox refused the username or password for reading mail ({text}). "
                                   "Check them, and that IMAP access is turned on in your mail settings (Zoho Mail: "
                                   "Settings, Mail accounts, IMAP access).")
        return MailError(RETRY, f"Your mail server refused ({text}).")
    return MailError(RETRY, f"Couldn't reach your mail server for reading mail ({text or type(exc).__name__}).")


class Inbox:
    """A read-only IMAP session: `with Inbox(mailbox) as inbox: ...`. Raises MailError."""

    def __init__(self, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> Inbox:
        host = self.mailbox.imap_host
        try:
            check_port(self.mailbox.imap_port, (IMAP_PORT,))
            self.conn = imaplib.IMAP4_SSL(host, IMAP_PORT, ssl_context=tls_context(), timeout=TIMEOUT)
            username, password = self.mailbox.reader
            try:
                self.conn.login(username, password)
            except UnicodeEncodeError:  # imaplib sends LOGIN as ASCII: AUTHENTICATE PLAIN in UTF-8 instead
                self.conn.authenticate("PLAIN", lambda _: f"\0{username}\0{password}".encode())
        except MailError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise _imap_error(exc, host) from None
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.conn is not None:
            with suppress(Exception):
                self.conn.logout()
            self.conn = None

    def _call(self, fn: Callable[..., tuple[str, list[Any]]], *args: Any) -> list[Any]:
        try:
            typ, data = fn(*args)
        except Exception as exc:
            raise _imap_error(exc, self.mailbox.imap_host) from None
        if typ != "OK":
            raise MailError(RETRY, f"Your mail server refused ({server_text(data[0] if data else typ)}).")
        return data

    def folders(self) -> dict[str, str]:
        """{"inbox": "INBOX", "junk": <the junk folder or "">, "sent": <the sent folder or "">} (RFC 6154 flags first,
        then the usual names)."""
        assert self.conn is not None
        found: dict[str, str] = {"inbox": "INBOX", "junk": "", "sent": ""}
        names: list[str] = []
        for line in self._call(self.conn.list):
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line or "")
            match = _LIST_LINE.match(text)
            if not match:
                continue
            name, flags = _unquote(match["name"]), match["flags"].lower()
            names.append(name)
            if "\\noselect" in flags or "\\nonexistent" in flags:
                continue
            if "\\junk" in flags and not found["junk"]:
                found["junk"] = name
            if "\\sent" in flags and not found["sent"]:
                found["sent"] = name
        lower = {n.lower(): n for n in names}
        for key, wanted in (("junk", JUNK_NAMES), ("sent", SENT_NAMES)):
            if not found[key]:
                found[key] = next((lower[w.lower()] for w in wanted if w.lower() in lower), "")
        return found

    def examine(self, folder: str = "INBOX") -> None:
        """Open a folder read-only (EXAMINE): a login check, and what fetch_new reads."""
        assert self.conn is not None
        self._call(self.conn.select, imap_quote(folder), True)

    def fetch_new(self, folder: str, state: dict[str, Any] | None, relevant: Callable[[Message], bool],
                  since: date | None = None, limit: int = INBOX_FETCH_MAX,
                  first_since: date | None = None) -> tuple[list[tuple[str, bytes]], dict[str, Any], bool]:
        """The messages that arrived in `folder` since the last check and that `relevant(headers)` wants, as
        (key, the first INBOUND_BYTES_MAX bytes), the folder's new state {"uidvalidity", "last_uid"}, and whether
        more new messages wait (more than `limit` arrived: call it again with the new state).

        The first check of a folder reads nothing: it notes where the folder is, so the mailbox's history is never
        imported; with `first_since` (OpenBerry read this mailbox with other details until then) it reads the mail
        since that day instead. When the folder's UIDVALIDITY changed (the server renumbered it), messages since
        `since` are read again; the caller handles each key once. Headers come first (BODY.PEEK[HEADER]); only the
        relevant messages are read in full, with BODY.PEEK, so nothing is marked as read.
        """
        assert self.conn is not None
        status = b" ".join(x for x in self._call(self.conn.status, imap_quote(folder), "(UIDVALIDITY UIDNEXT)")
                           if isinstance(x, bytes)).decode("ascii", "replace")
        validity = int(m.group(1)) if (m := re.search(r"UIDVALIDITY (\d+)", status)) else 0
        uidnext = int(m.group(1)) if (m := re.search(r"UIDNEXT (\d+)", status)) else 0
        previous = state or {}
        if (not previous or "last_uid" not in previous) and first_since is None:
            return [], {"uidvalidity": validity, "last_uid": max(0, uidnext - 1)}, False
        if not previous or "last_uid" not in previous:
            previous, since = {"uidvalidity": -1, "last_uid": 0}, first_since  # read like a renumbered folder
        last = int(previous.get("last_uid") or 0)
        renumbered = int(previous.get("uidvalidity") or 0) != validity
        if not renumbered and uidnext and uidnext - 1 <= last:
            return [], {"uidvalidity": validity, "last_uid": last}, False
        self.examine(folder)
        if renumbered:
            found = self._uids(self._call(self.conn.uid, "SEARCH", "SINCE", imap_date(since or date.today())))
            last = 0
        else:
            found = [u for u in self._uids(self._call(self.conn.uid, "SEARCH", f"UID {last + 1}:*")) if u > last]
        uids = sorted(set(found))[:limit]
        more = len(set(found)) > limit
        new_last = max([last, *uids]) if more else max([last, *uids, uidnext - 1])
        wanted: list[int] = []
        for start in range(0, len(uids), FETCH_BATCH):
            batch = uids[start:start + FETCH_BATCH]
            for uid, raw in self._fetch(batch, "BODY.PEEK[HEADER]"):
                try:
                    headers = email.message_from_bytes(raw, policy=email.policy.default)
                    if relevant(headers):
                        wanted.append(uid)
                except Exception:  # a header nobody can parse: not ours to read
                    continue
        out: list[tuple[str, bytes]] = []
        for start in range(0, len(wanted), FETCH_BATCH):
            for uid, raw in self._fetch(wanted[start:start + FETCH_BATCH], f"BODY.PEEK[]<0.{INBOUND_BYTES_MAX}>"):
                out.append((f"uid:{folder}:{validity}:{uid}", raw))
        return out, {"uidvalidity": validity, "last_uid": new_last}, more

    @staticmethod
    def _uids(data: list[Any]) -> list[int]:
        text = b" ".join(x for x in data if isinstance(x, bytes)).decode("ascii", "replace")
        return [int(x) for x in text.split() if x.isdecimal()]

    def _fetch(self, uids: list[int], part: str) -> list[tuple[int, bytes]]:
        if not uids:
            return []
        assert self.conn is not None
        data = self._call(self.conn.uid, "FETCH", ",".join(str(u) for u in uids), f"(UID {part})")
        out = []
        waiting: bytes | None = None  # a message whose UID comes after it ("* 1 FETCH (BODY[] {n}... UID 5)")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                meta = item[0].decode("ascii", "replace") if isinstance(item[0], bytes) else str(item[0])
                waiting = None
                if m := re.search(r"UID (\d+)", meta):
                    out.append((int(m.group(1)), item[1][:INBOUND_BYTES_MAX]))
                else:
                    waiting = item[1][:INBOUND_BYTES_MAX]
            elif isinstance(item, bytes) and waiting is not None:
                if m := re.search(rb"UID (\d+)", item):
                    out.append((int(m.group(1)), waiting))
                waiting = None
        return out

    def append_sent(self, folder: str, raw: bytes) -> None:
        """Put a copy of a sent email in the Sent folder, marked as read (mail apps do this themselves; SMTP
        doesn't)."""
        assert self.conn is not None
        self._call(self.conn.append, imap_quote(folder), "(\\Seen)", imaplib.Time2Internaldate(time.time()), raw)


def check_login(mailbox: Mailbox) -> None:
    """Sign in over IMAP and open the inbox read-only (the Mailbox page's Check inbox). Raises MailError."""
    with Inbox(mailbox) as inbox:
        inbox.examine("INBOX")


def save_sent_copy(mailbox: Mailbox, raw: bytes) -> str:
    """APPEND a sent email to the Sent folder; "" when it worked, else why not (it never changes the sent status)."""
    try:
        with Inbox(mailbox) as inbox:
            folder = inbox.folders()["sent"]
            if not folder:
                return "Couldn't find your Sent folder."
            inbox.append_sent(folder, raw)
    except MailError as exc:
        return exc.text
    return ""


# --------------------------------------------------------------------------------------
# Reading what came back: replies, opt-outs, auto-replies and bounces
# --------------------------------------------------------------------------------------

REPLY = "reply"
OPT_OUT = "opt_out"
AUTO_REPLY = "auto_reply"
SOFT_BOUNCE = "soft_bounce"
POLICY_REJECT = "policy_reject"
INBOUND_KINDS = (REPLY, OPT_OUT, AUTO_REPLY, BOUNCE, SOFT_BOUNCE, POLICY_REJECT)
_MESSAGE_IDS = re.compile(r"<[^<>\s]{3,250}>")
_SYSTEM_SENDER = re.compile(r"^(?:mailer[-_.]?daemon|postmaster|mail[-_.]?delivery[-_.]?subsystem|"
                            r"microsoftexchange\w*)$", re.I)
_BOUNCE_SUBJECT = re.compile(r"undeliver|delivery status notification|delivery (?:has )?failed|mail delivery failed|"
                             r"returned mail|delivery failure|failure notice|non[- ]?delivery|could not be delivered|"
                             r"not delivered", re.I)
_AUTO_SUBJECT = re.compile(r"^\s*(?:automatic reply|auto(?:matic)?[- ]?reply|auto:|autoreply|out of (?:the )?office|"
                           r"ooo\b|abwesenheitsnotiz|r[ée]ponse automatique|respuesta autom[áa]tica|رد تلقائي|"
                           r"خارج المكتب|في إجازة|في اجازة)", re.I)
_UNSUBSCRIBE_SUBJECT = re.compile(r"^\s*unsubscribe\s+([0-9a-f]{32})\b", re.I)
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|tr|رد|إعادة توجيه)\s*:\s*)+", re.I)


def base_subject(subject: str) -> str:
    """A subject without its Re:/Fw: prefixes, folded: a reply or forward and the email it answers compare equal."""
    return " ".join(_PREFIX.sub("", subject or "").split()).casefold()


@lru_cache(maxsize=8192)
def _fold_char(ch: str) -> str:
    ch = {"’": "'", "‘": "'", "“": '"', "”": '"', "ـ": ""}.get(ch, ch)
    return "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)).casefold()


def fold(text: str) -> str:
    """Case, accents and Arabic hamza or vowel marks folded away, curly quotes made straight, for phrase matching."""
    return "".join(_fold_char(ch) for ch in text or "")


def _fold_map(text: str) -> tuple[str, list[int]]:
    """fold(text), and where in `text` each of its characters comes from: a match is shown as it was written."""
    parts: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text):
        folded = _fold_char(ch)
        parts.append(folded)
        where += [i] * len(folded)
    return "".join(parts), where


# --------------------------------------------------------------------------------------
# Opt-outs. Any reply already stops the lead's sequence (repo.record_inbound logs it), so only a reply that says
# nothing but "unsubscribe", "stop", "remove me", "don't email me again", "please take me off your list" (or the
# Arabic for them) acts by itself: SURE, the address goes on the never-email list, for every company. A chauffeur
# company's clients write "stop at the mall", "remove me from tomorrow's pickup list" or "please don't send the car
# before 9" in the middle of a booking: a reply that only looks like an opt-out (MAYBE) stays a reply, flagged for the
# user to decide. Patterns are matched on folded text (fold: إلغاء -> الغاء, أزلني -> ازلني).
# --------------------------------------------------------------------------------------

SURE, MAYBE = "sure", "maybe"
OPT_OUT_TEXT_MAX = 200  # the most a sure opt-out says (its opt-out words, without greetings and sign-offs)

# Right after a negation ("please don't unsubscribe me", "I don't want to unsubscribe") a phrase doesn't count.
_NEGATED = r"(?<!not )(?<!n't )(?<!dont )(?<!never )(?<!t want to )(?<!t wish to )(?<!t like to )"
# The end of a clause: "remove me." "stop sending, please" "take me off now".
_END = r"(?= *(?:[.!?,;:)\n]|$| please| pls| now| thanks?| thank you| asap| again| immediately))"
# Words of a trip, not a mailing list: "remove me from tomorrow's pickup list", "take us off the waiting list".
_TRIP = (r"(?:pick-?ups?|guests?|waiting|bookings?|reservations?|transfers?|trips?|airport|drivers?|passengers?|"
         r"manifest|schedule|roster|flights?|cars?|vehicles?|quotes?|pending|rides?|tomorrow'?s?|today'?s?|"
         r"tonight'?s?|meet and greet)")
_WORDS = r"(?:[^\s.,;!?]+ ){0,3}?"
_LISTED = (r"(?!" + _WORDS + _TRIP + r"\b)" + _WORDS
           + r"(?:list|lists|database|records|mailings|e-?mails|contacts|newsletters?|crm|system)\b"
           + r"(?! (?:of|for) (?:[^\s.,;!?]+ ){0,2}?" + _TRIP + ")")
_WHO = r"(?:me|us|him|her|them)"
_MINE = (r"(?:my (?:e-?mail(?: address)?|address|details|data|contact(?: details)?|name)|"
         r"our (?:e-?mail(?: address)?|address|details|names?)|this (?:e-?mail )?address|"
         r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})")
# "don't contact me on my mobile", "do not contact me before 9am", "don't email me the invoices", "please don't email
# me here, send it to...": a channel, a time or another object follows, so it is about the booking, not the emails.
_ELSEWHERE = (r"(?! (?:on|via|by|through|before|after|until|till|at|during|the|my|our|his|her|their|your|here|"
              r"directly|about|regarding|with|if|when|for|from|in person|privately|instead)\b)")
_OBJECT = r" (?:me|us|him|her|them|this (?:e-?mail )?address)"
_AR_ELSEWHERE = r"(?! (?:على|عبر|قبل|بعد|في|من خلال|بالهاتف|بالواتساب|هاتفيا|هنا))"

# What looks like an opt-out, anywhere in the new text (MAYBE). The footer's own instructions ('reply "unsubscribe"',
# "click unsubscribe", "the unsubscribe link") and leaving a service ("unsubscribe from the monthly package") don't.
_LOOKS_LIKE = (
    r"(?<!reply \")(?<!reply ')(?<!word \")(?<!with \")(?<!click )(?<!click on )(?<!press )(?<!the )(?<!an )"
    r"(?<!here to )(?<!link to )(?<!below to )"
    r"unsubscribe(?! (?:link|button|page|option))(?! from (?:our|my) )"
    r"(?! from (?:the|this|that) (?!(?:mailing |e-?mail )?(?:list|e-?mails?|newsletters?|mailings?)\b))",
    r"unsub",
    r"opt " + _WHO + r" out",
    r"(?<!\" to )(?<!' to )opt[- ]?out" + _END,
    r"opt[- ]?out of (?:these|this|your|all|any|future|further|the|such) (?:mailing |e-?mail |marketing )?"
    r"(?:e-?mails?|messages?|mailings?|list|lists|communications?|newsletters?)",
    r"(?:remove|delete|erase) (?:" + _WHO + "|" + _MINE + r") from " + _LISTED,
    r"(?:be|get|been) removed from " + _LISTED,
    r"remove from (?:your |the |this |all )?(?:mailing |e-?mail |contact )?(?:list|lists|database)",
    r"remove (?:me|us)" + _END,
    r"remove my (?:e-?mail )?address" + _END,
    r"take (?:" + _WHO + "|" + _MINE + r") off(?:" + _END + r"| (?:your|the|this|all|any) " + _LISTED + ")",
    r"(?:do not|don't|dont|never) (?:contact|e-?mail|message|write to|send e-?mails? to|send messages? to)"
    r"(?: (?:" + _WHO + r"|this (?:e-?mail )?address|my (?:e-?mail(?: address)?|address)))" + _ELSEWHERE,
    r"(?:do not|don't|dont|never) (?:contact|e-?mail|message|write(?: to)?)(?: (?:me|us|this (?:e-?mail )?address))?"
    r" (?:again|any ?more|ever again)",
    r"(?:do not|don't|dont) contact(?= *(?:[.!?\n]|$))",
    r"stop (?:e-?mailing|contacting|messaging|writing to|spamming|mailing)(?:" + _OBJECT + _ELSEWHERE
    + r"|(?! (?:me|us|him|her|them|this|the|our|my|his|her|their|your|on|via|at|before|after)\b))",
    r"stop sending(?: " + _WHO + r")?(?: (?:any )?(?:more )?(?:e-?mails?|messages?|mailings?|newsletters?|marketing|"
    r"offers?|promotions?)\b| (?:these|this|them|those|it)(?= *(?:[.!?,;:)\n]|$| please| now| e-?mails?| messages?| thanks?))|" + _END + ")",
    r"no more e-?mails?(?! (?:needed|required|necessary|for now|until|till)\b)",
    r"(?:do not|don't|dont) send (?:me |us )?(?:any )?(?:more|further) (?:e-?mails?|messages?|mailings?|newsletters?|"
    r"marketing)",
    r"(?:do not|don't|dont) send (?:me|us) (?:any )?(?:e-?mails?|messages?|mailings?)(?: any ?more| again| in (?:the )?"
    r"future)",
    r"(?:i|we) (?:do not|don't|dont|no longer|do no longer) (?:wish|want|like) to (?:receive|get|hear from you|be "
    r"contacted|be e-?mailed)",
    r"(?:i|we)(?:'d| would) (?:like|want|prefer) (?:not to|to not) (?:receive|get|be contacted|be e-?mailed)",
    r"(?:cease|stop|end) all (?:communications?|contact|e-?mails?|correspondence)",
    r"refrain from (?:contacting|e-?mailing|messaging|writing to)",
    r"delete (?:my|our|this) (?:e-?mail(?: address)?|details|data|contact(?: details)?|address|information)",
    r"(?:i|we) never (?:subscribed|signed up|opted in|asked (?:for|to receive) (?:this|these|your))",
    # Arabic
    r"(?:إلغاء|الغاء) الاشتراك(?! (?:الشهري|السنوي|الأسبوعي|اليومي|للسيارات|في الباقة|في خدمة|في الخدمة))",
    r"(?:ألغوا|ألغي|إلغاء|ألغ) (?:اشتراكي|اشتراكنا)",
    r"لا (?:تراسلني|تراسلوني|تراسلونا|تراسلنا|تراسلوننا)",
    r"عدم (?:مراسلتي|مراسلتنا)",
    r"عدم (?:التواصل|الاتصال) (?:معي|معنا|بي|بنا)" + _AR_ELSEWHERE,
    r"(?:أزلني|إزالتي|أزيلوني|احذفوني|احذفني|أزيلونا|احذفونا|امسحوني)"
    r"(?! من قائمة (?:الركاب|الضيوف|الانتظار|الحجوزات|الحجز|المسافرين|الاستقبال|التوصيل))",
    r"(?:إزالة|حذف|شطب|مسح) (?:بريدي|ايميلي|إيميلي|عنواني|بياناتي|اسمي)(?: الإلكتروني| الالكتروني)?",
    r"لا ترسل(?:وا|ون|ي)? (?:لي|لنا|إلي|إلينا) (?:أي |المزيد من )?(?:رسائل|ايميلات|إيميلات|بريد|ايميل|إيميل|شيئا|شيء)"
    r"(?! (?:نصية|sms|على الواتساب|واتساب|عبر الواتساب))",
    r"لا ترسل(?:وا|ون)? (?:أي |المزيد من )?(?:رسائل|ايميلات|إيميلات|بريد)(?: أخرى)? (?:مرة أخرى|بعد الآن|ثانية|مجددا)",
    r"لا (?:نرغب|أرغب|نريد|أريد) (?:في )?(?:استلام|تلقي|استقبال) (?:أي )?(?:رسائل|ايميلات|إيميلات|بريد|مراسلات|عروض)",
    r"لا تتواصل(?:وا|ون)? (?:معي|معنا)" + _AR_ELSEWHERE,
    r"توقف(?:وا)? عن (?:مراسلتي|مراسلتنا|إرسال (?:الرسائل|رسائل|الايميلات|ايميلات|الإيميلات|إيميلات|البريد))",
    r"(?:عدم|بدون) إرسال (?:أي )?(?:رسائل|ايميلات|إيميلات|بريد)",
    r"(?:أرجو|الرجاء|يرجى|نرجو|رجاء) عدم (?:الإرسال|المراسلة|مراسلتنا|مراسلتي|التواصل|الاتصال)" + _AR_ELSEWHERE,
)
OPT_OUT_PATTERNS = tuple(re.compile(_NEGATED + r"(?<!\w)(?:" + fold(p) + r")(?!\w)") for p in _LOOKS_LIKE)

# Polite words around an opt-out ("Please", "kindly", "could you", "thanks", "now").
_LEAD_IN = fold(r"(?:(?:i|we)(?:'d| would)? (?:like|want|wish|need) (?:you )?to |(?:can|could|would|will) you (?:please "
                r"|kindly )?|(?:just |and )?)(?:please |pls |plz |kindly |just )?(?:الرجاء |رجاء |يرجى |أرجو |نرجو "
                r"|من فضلك |لو سمحت )?")
_LEAD_OUT = fold(r"(?: *(?:please|pls|plz|now|thanks|thank you|thx|asap|immediately|right away|right now|الرجاء|رجاء|"
                 r"من فضلك|لو سمحت|شكرا|فورا))*")
# What a sure opt-out says, as a whole sentence (with the polite words around it).
_SURE_SAYS = (
    r"unsubscribe(?: me| us)?(?: from (?:this|your|the|all|any|these) (?:mailing |e-?mail |marketing )?(?:list|lists|"
    r"e-?mails|mailings))?",
    r"unsub",
    r"stop(?: it| that| this| now)?",
    r"stop (?:e-?mailing|contacting|messaging|writing to|spamming|mailing)(?: (?:me|us|this (?:e-?mail )?address))?",
    r"stop sending(?: (?:me|us))?(?: (?:any )?(?:more )?(?:e-?mails?|messages?|mailings?)| (?:these|this|them|those)"
    r"(?: e-?mails?| messages?)?)?(?: to (?:me|us|this (?:e-?mail )?address))?",
    r"remove(?: (?:me|us|this|" + _MINE + r"))?(?: from (?:this|your|the|all|any|these|future|further)(?: (?:mailing "
    r"|e-?mail |contact |distribution |marketing )?(?:list|lists|database|records|e-?mails|mailings|communications?))?)?",
    r"(?:be )?removed from (?:this|your|the|all|any) (?:mailing |e-?mail |contact |distribution |marketing )?"
    r"(?:list|lists|database|records)",
    r"remove from (?:your |the |this |all )?(?:mailing |e-?mail )?list",
    r"take (?:me|us|" + _MINE + r") off(?: (?:this|your|the|all|any) (?:mailing |e-?mail |contact |distribution "
    r"|marketing )?(?:list|lists|database|records|e-?mails))?",
    r"opt(?: me| us)?[- ]?out(?: of (?:this|these|your|all|any|future|further|the) (?:mailing |e-?mail |marketing )?"
    r"(?:list|lists|e-?mails?|messages?|mailings?|communications?))?",
    r"(?:do not|don't|dont|never) (?:contact|e-?mail|message|write to|send e-?mails? to)(?: (?:me|us|this (?:e-?mail )?"
    r"address|my (?:e-?mail(?: address)?|address)))?(?: (?:again|any ?more|ever again|in (?:the )?future))?",
    r"(?:do not|don't|dont) send (?:me |us )?(?:any )?(?:more |further )?(?:e-?mails?|messages?|mailings?)"
    r"(?: (?:any ?more|again|in (?:the )?future))?",
    r"no more e-?mails?(?: (?:from you|to (?:me|us|this (?:e-?mail )?address)))?",
    r"(?:i|we) (?:do not|don't|dont|no longer) (?:wish|want) to (?:receive|get) (?:any )?(?:more |further )?"
    r"(?:e-?mails?|messages?|communications?|correspondence|mailings?)(?: from (?:you|your company|you guys))?",
    r"(?:cease|stop) all (?:communications?|contact|e-?mails?|correspondence)",
    r"refrain from (?:contacting|e-?mailing|messaging) (?:me|us)",
    r"delete (?:my|our) (?:e-?mail(?: address)?|details|data|address|contact details)(?: from your (?:list|database"
    r"|records|system))?",
    # Arabic
    r"(?:إلغاء الاشتراك|(?:ألغوا|ألغي|إلغاء) (?:اشتراكي|اشتراكنا))",
    r"(?:توقف|توقفوا|أوقف|أوقفوا|كفى|كفاية)",
    r"(?:أزلني|إزالتي|أزيلوني|احذفوني|احذفني)(?: من (?:قائمتكم|قائمتك|القائمة|القائمة البريدية|قائمة المراسلات|"
    r"قائمة البريد|مراسلاتكم|قائمة مراسلاتكم))?",
    r"(?:إزالة|حذف) (?:بريدي|ايميلي|إيميلي|عنواني)(?: الإلكتروني| الالكتروني)?(?: من (?:قائمتكم|قائمتك|القائمة|قوائمكم)"
    r"(?: البريدية)?)?",
    r"لا (?:تراسلني|تراسلوني|تراسلونا|تراسلنا)(?: (?:مرة أخرى|مرة ثانية|بعد الآن|ثانية|أبدا))?",
    r"عدم (?:مراسلتي|مراسلتنا|الإرسال|التواصل معي|التواصل معنا|الاتصال بي|الاتصال بنا)"
    r"(?: (?:مرة أخرى|مرة ثانية|بعد الآن|ثانية))?",
    r"عدم إرسال (?:أي )?(?:رسائل|ايميلات|إيميلات)(?: أخرى)?(?: (?:مرة أخرى|بعد الآن))?",
    r"لا ترسل(?:وا|ون|ي)? (?:لي |لنا |إلي |إلينا )?(?:أي |المزيد من )?(?:رسائل|ايميلات|إيميلات|بريد|شيئا|شيء)"
    r"(?: أخرى)?(?: (?:مرة أخرى|مرة ثانية|بعد الآن|بعد اليوم|مجددا|مستقبلا))?",
    r"لا (?:نرغب|أرغب) في (?:استلام|تلقي) (?:أي )?(?:رسائل|ايميلات|إيميلات|بريد|مراسلات)(?: منكم| منك)?"
    r"(?: (?:مستقبلا|بعد الآن|مرة أخرى))?",
    r"لا تتواصل(?:وا)? (?:معي|معنا)(?: (?:مرة أخرى|بعد الآن))?",
    r"توقف(?:وا)? عن (?:مراسلتي|مراسلتنا|إرسال (?:الرسائل|رسائل|الايميلات|ايميلات|البريد))",
)
_SAYS = "(?:" + "|".join(fold(p) for p in _SURE_SAYS) + ")"
# One opt-out, or two joined: "please unsubscribe me and stop emailing", "الرجاء إلغاء الاشتراك وعدم مراسلتي".
_SURE = re.compile(_LEAD_IN + _SAYS + "(?:(?: and (?:please )?| و ?)" + _SAYS + ")*" + _LEAD_OUT)
# Sentences that say nothing either way: greetings, thanks, "not interested", sign-offs, a phone's footer.
_FILLER = re.compile(fold("|".join((
    r"(?:hi|hello|hey|dear|good (?:morning|afternoon|evening|day)|greetings|مرحبا|أهلا|السلام عليكم|عزيزي|عزيزتي|"
    r"الأستاذ|السيد|السيدة)(?: [^\s]+){0,3}",
    r"(?:many |a lot of )?(?:thanks|thank you|thx|ty|cheers)(?: (?:very much|so much|a lot|in advance|again))?"
    r"(?: for (?:your|the) (?:e-?mail|message|offer|note|time))?",
    r"no|nope|sorry|no thanks|no thank you|(?:we are |we're |i am |i'm )?not interested(?: at all)?|not for us",
    r"(?:i|we) never (?:subscribed|signed up|opted in|asked for (?:this|these|it|your e-?mails))(?: to (?:this|these|"
    r"your (?:list|e-?mails)))?|this is spam|spam|who gave you (?:my|our) (?:e-?mail(?: address)?|address|details)",
    r"(?:best |kind |warm |many )?regards|best|sincerely|br|thanks (?:and|&) regards|yours(?: truly| sincerely)?",
    r"sent from my [^\s]+(?: [^\s]+){0,2}|get outlook for (?:ios|android)|sent from (?:outlook|yahoo mail|mail for "
    r"windows|gmail)(?: for [^\s]+)?",
    r"شكرا|شكرا جزيلا|شكرا لك|شكرا لكم|مع الشكر|مع التحية|تحياتي|مع خالص التحية|وشكرا|لا شكرا|غير مهتم|غير مهتمين|"
    r"لسنا مهتمين|لست مهتما|لا نحتاج|لا",
))))
_SIGN_OFF = re.compile(fold(r"(?:(?:best |kind |warm |many )?regards|best|sincerely|cheers|thanks|thank you|br|"
                            r"thanks (?:and|&) regards|مع التحية|تحياتي|مع خالص التحية|مع الشكر|شكرا|وشكرا)"))
_PHONE = re.compile(r"(?:(?:tel|mob|mobile|phone|cell|m|t|whatsapp)\.?:? *)?\+?[\d ()./-]{7,}")
# A sentence that is only "stop", "unsubscribe", "remove"... (MAYBE when there is more around it).
_ALONE = re.compile(_LEAD_IN + "(?P<word>" + fold(r"stop|stop it|unsubscribe|unsub|remove|remove me|remove us|opt me out|"
                                                  r"opt out|opt-out|توقف|توقفوا|أوقف|أوقفوا|كفى|كفاية|أزلني|إزالتي|"
                                                  r"إلغاء الاشتراك") + ")" + _LEAD_OUT)
_ABBREVIATION = re.compile(r"\b(mr|mrs|ms|dr|st|eng)\.", re.I)
_SPLIT = re.compile(r"[.!?؟;](?=\s|$)|[,،\n]")
_PART_STRIP = " \t,،;:-–—*\"'()[]<>…!?.؟"


def _without_footers(text: str, footers: Iterable[str]) -> str:
    """The text without our footers, should a mail app have left our email in the reply unmarked. A footer is removed
    only as a whole (all its lines in a row), so a footer that ends in a line "Unsubscribe" never swallows a reply that
    says just that; and never when nothing would be left."""
    lines = (text or "").split("\n")
    norm = [" ".join(line.split()).casefold() for line in lines]
    keep = [True] * len(lines)
    for footer in footers:
        want = [" ".join(line.split()).casefold() for line in (footer or "").splitlines() if line.strip()]
        filled = [i for i, n in enumerate(norm) if n and keep[i]]
        for start in range(len(filled) - len(want) + 1) if want else ():
            run = filled[start:start + len(want)]
            if [norm[i] for i in run] == want:
                for i in run:
                    keep[i] = False
                break
    rest = "\n".join(line for line, k in zip(lines, keep, strict=True) if k)
    return rest if rest.strip() else text


def _parts(text: str) -> list[str]:
    """Sentences and clauses (split at full stops, commas and line ends), trimmed."""
    text = _ABBREVIATION.sub(r"\1", text)
    return [p for p in (" ".join(x.split()).strip(_PART_STRIP) for x in _SPLIT.split(text)) if p]


def _says_nothing(folded: str, names: set[str]) -> bool:
    return bool(_FILLER.fullmatch(folded) or _PHONE.fullmatch(folded)
                or (names and set(re.findall(r"[^\W\d_]+", folded)) <= names))


def _sure_words(body: str, extra: list[str], names: set[str]) -> str:
    """The opt-out words when the reply says nothing else (greetings, thanks, sign-offs and the sender's name aside),
    else "". `extra`: what the sender put in the subject."""
    said: list[str] = []
    other = 0
    content = False
    for line in body.split("\n"):
        folded_line = fold(" ".join(line.split()).strip(_PART_STRIP))
        if content and _SIGN_OFF.fullmatch(folded_line):
            break  # "Regards," after the message: a signature follows
        for part in _parts(line):
            folded = fold(part)
            if _says_nothing(folded, names):
                continue
            content = True
            if _SURE.fullmatch(folded):
                said.append(part)
            else:
                other += 1
    for part in extra:
        folded = fold(part)
        if _SURE.fullmatch(folded):
            said.append(part)
        elif not _says_nothing(folded, names):
            other += 1
    if said and not other and len(said) <= 4 and sum(len(p) for p in said) <= OPT_OUT_TEXT_MAX:
        return said[0]
    return ""


def _looks_like(text: str) -> str:
    """The words that look like an opt-out, as written ("" if none)."""
    folded, where = _fold_map(text)
    for pattern in OPT_OUT_PATTERNS:
        if m := pattern.search(folded):
            return text[where[m.start()]:where[m.end() - 1] + 1] if m.end() > m.start() else ""
    for part in _parts(text):
        if _ALONE.fullmatch(fold(part)):
            return part
    return ""


def read_opt_out(text: str, subject: str = "", ignore: Iterable[str] = (), names: Iterable[str] = ()) -> tuple[str,
                                                                                                                str]:
    """Whether a reply's new text asks not to be emailed again: (SURE, words) when it says nothing else, (MAYBE,
    words) when it only looks like it, ("", "") when not. Words are given as they were written.

    `ignore`: our own footers (removed only as a whole). `names`: the sender's name ("Regards, Noura" says nothing).
    The subject counts when the sender wrote it ("Unsubscribe", "Remove me"); a "Re: ..." subject is our own email's,
    except what was typed after it ("Re: Airport transfers - UNSUBSCRIBE")."""
    body = _without_footers((text or "").replace("\r\n", "\n"), ignore)
    lines = body.split("\n")
    if (cut := next((i for i, line in enumerate(lines) if line.rstrip() in ("--", "-- ")), None)) is not None:
        body = "\n".join(lines[:cut])  # the signature after "-- " says nothing about what they want
    names_folded = {w for n in names for w in re.findall(r"[^\W\d_]+", fold(n))}
    subject = " ".join((subject or "").split())
    own = bool(subject) and not _PREFIX.match(subject)
    pieces = re.split(r"\s+[-–—|]+\s+", _PREFIX.sub("", subject)) if subject and not own else []
    typed = pieces[-1] if len(pieces) > 1 else ""  # typed after our subject
    extra = [subject] if own else ([typed] if typed and _SURE.fullmatch(fold(typed.strip(_PART_STRIP))) else [])
    if words := _sure_words(body, [p.strip(_PART_STRIP) for p in extra], names_folded):
        return SURE, words.strip(_PART_STRIP)
    for candidate in ([subject] if own else []) + [body] + ([typed] if typed else []):
        if words := _looks_like(candidate):
            return MAYBE, words.strip(_PART_STRIP)
    return "", ""


def opt_out_words(text: str, subject: str = "", ignore: Iterable[str] = ()) -> str:
    """The words in a reply that ask not to be emailed again, or look like it ("" if none): read_opt_out's words."""
    return read_opt_out(text, subject, ignore)[1]


_ON_WROTE = re.compile(r"^on\b.{0,300}\bwrote:\s*$", re.I)
_QUOTE_START = (
    re.compile(r"^-{2,}\s*(?:original message|forwarded message|الرسالة الأصلية)\s*-{2,}", re.I),
    # Outlook's separator is 32 underscores; a booking form's "____" line between its fields is not one.
    re.compile(r"^_{30,}\s*$"),
    _ON_WROTE,
    re.compile(r"^le\b.{0,300}\ba [ée]crit\s*:\s*$", re.I),
    re.compile(r"^am\b.{0,300}\bschrieb\b.{0,200}:\s*$", re.I),
    # Zoho Mail: "---- On Mon, 12 Oct 2026 10:02:11 +0400 Sam Rahman <sam@...> wrote ---", often over an unmarked quote.
    re.compile(r"^-{2,}\s*on\b.{0,300}\bwrote\s*:?\s*-*\s*$", re.I),
    # Arabic: "في ...، كتب سام <sam@...>:", always with a date or an address. The whole word only: "المكتب:" (the
    # office) is not it, nor a trip request's "نحتاج نقل كتب ومعدات معرض الكتاب:" (books).
    re.compile(r"^(?=.*[\d@]).{0,300}(?<!\w)كتب(?!\w).{0,300}:\s*$"),
)
STRIP_INPUT_MAX = 50_000  # the start of a long message is enough to find what is new
# Outlook's quoted header block, in the languages its users write in: From/Sent/To/Subject.
_HEADER_BLOCK = re.compile(r"^(?:from|de|von|da|من|от)\s*:", re.I)
_HEADER_NEXT = re.compile(r"^(?:sent|date|envoy[ée]|gesendet|to|subject|enviado(?: el)?|para|asunto|inviato|a|oggetto|"
                          r"отправлено|кому|тема|بتاريخ|التاريخ|إلى|الموضوع)\s*:", re.I)
_HEADER_SUBJECT = re.compile(r"^(?:subject|objet|betreff|asunto|oggetto|тема|الموضوع)\s*:", re.I)
_BIDI = "‎‏‪‫‬‭‮⁦⁧⁨⁩"  # direction marks Gmail adds to Arabic


def strip_quoted(text: str) -> str:
    """The new part of a reply: quoted lines (">") and everything from "On ... wrote:", "-----Original Message-----",
    an Outlook From:/Sent: block or an Arabic "كتب ...:" line on are removed.

    A From: line starts a quoted header block only with an address in it or a Subject: line just below: a trip
    request's "From: Dubai Marina / To: DXB Terminal 3 / Date: ..." is the reply itself."""
    lines = (text or "")[:STRIP_INPUT_MAX].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip().strip(_BIDI).strip()
        joined = f"{stripped} {lines[i + 1].strip().strip(_BIDI).strip()}" if i + 1 < len(lines) else stripped
        if len(stripped) <= 600 and any(p.match(stripped) for p in _QUOTE_START) or (
                stripped.lower().startswith("on ") and _ON_WROTE.match(joined)):
            break
        if (_HEADER_BLOCK.match(stripped) and any(_HEADER_NEXT.match(n.strip()) for n in lines[i + 1:i + 4])
                and ("@" in stripped or any(_HEADER_SUBJECT.match(n.strip()) for n in lines[i + 1:i + 6]))):
            break
        if stripped.startswith(">"):
            continue
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


class _TextFromHtml(HTMLParser):
    """Visible text of an HTML email, without quoted replies (blockquote, Gmail's and Outlook's quote blocks).
    Nothing is ever loaded: no images, links or styles."""

    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "hr"}
    SKIP = {"script", "style", "head", "title", "blockquote"}
    VOID = {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr", "source", "embed"}

    QUOTE_MARKERS = ("gmail_quote", "moz-cite-prefix", "yahoo_quoted")
    # Outlook (web, Outlook.com, desktop) puts only the From/Sent/To/Subject block in divRplyFwdMsg (after the
    # appendonsend marker): the quoted email follows as its sibling. Everything from there on is the quote.
    END_MARKERS = ("divrplyfwdmsg", "appendonsend")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_tag = ""   # the element being skipped; only its own nesting is counted (other tags may not close)
        self.skip_depth = 0
        self.done = False    # an END_MARKER was seen: the rest is quoted

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if self.skip_depth:
            self.skip_depth += tag == self.skip_tag
            return
        marker = " ".join(v or "" for k, v in attrs if k in ("class", "id")).lower()
        if any(m in marker for m in self.END_MARKERS):
            self.done = True
            return
        if tag in self.VOID:
            if tag in self.BLOCK:
                self.parts.append("\n")
            return
        if tag in self.SKIP or any(m in marker for m in self.QUOTE_MARKERS):
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):  # table cells side by side: "Flight BA 107", not "FlightBA 107"
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= tag == self.skip_tag
            return
        if tag in self.BLOCK and tag not in self.VOID and not self.done:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and not self.done:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextFromHtml()
    with suppress(Exception):
        parser.feed(html or "")
        parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@dataclass(frozen=True)
class Inbound:
    """What an inbox message is (classify_inbound). Written by strangers: data, never instructions."""

    kind: str
    from_email: str = ""
    subject: str = ""
    message_id: str = ""                 # its own Message-ID: the key that handles it once
    references: tuple[str, ...] = ()     # In-Reply-To and References: one of them may be ours
    text: str = ""                       # the new text (quoted part removed), at most INBOUND_TEXT_MAX
    token: str = ""                      # the List-Unsubscribe token in an "unsubscribe <token>" subject
    failed_recipients: tuple[str, ...] = ()
    status: str = ""                     # a bounce's status, e.g. 5.1.1
    diagnostic: str = ""
    original_ids: tuple[str, ...] = ()   # a bounce: the Message-IDs of the email that bounced
    opt_out: str = ""                    # the words that made it an opt-out (as written)
    opt_out_hint: str = ""               # a reply: words that look like an opt-out, for the user to decide
    mentioned: tuple[str, ...] = ()      # addresses anywhere in it, quoted part and To/Cc included (whom a forward is about)


def _header(msg: Message, name: str, whole: bool = False) -> str:
    """A header as one line of text ("" when missing or unreadable). Shortened unless `whole` (References may
    list many Message-IDs)."""
    try:
        value = msg.get(name)
    except Exception:  # a header the email library can't parse
        return ""
    if value is None:
        return ""
    return " ".join(str(value).split()) if whole else server_text(str(value))


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        if isinstance(content, str):
            return content
    except Exception:
        pass
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(part.get_content_charset() or "utf-8", "replace") if _known_charset(part) \
            else payload.decode("utf-8", "replace")
    return ""


def _known_charset(part: Message) -> bool:
    import codecs

    try:
        codecs.lookup(part.get_content_charset() or "utf-8")
    except LookupError:
        return False
    return True


def _body_text(msg: Message) -> str:
    try:
        body = msg.get_body(preferencelist=("plain", "html"))  # type: ignore[attr-defined]
    except Exception:
        body = None
    if body is None:
        return ""
    text = _part_text(body)
    return html_to_text(text) if body.get_content_type() == "text/html" else text


def _all_text(msg: Message) -> str:
    """Every text part, for bounces from systems that don't follow RFC 3464."""
    parts = []
    for part in msg.walk():
        if part.get_content_maintype() == "text" or part.get_content_type() in (
                "message/delivery-status", "message/rfc822", "message/global-delivery-status"):
            parts.append(part.as_string() if part.get_content_maintype() == "message" else _part_text(part))
    return "\n".join(parts)


def _dsn_blocks(msg: Message) -> list[dict[str, str]]:
    """The per-recipient fields of an RFC 3464 delivery-status part: [{"recipient", "action", "status",
    "diagnostic"}]."""
    blocks: list[dict[str, str]] = []
    for part in msg.walk():
        if part.get_content_type() not in ("message/delivery-status", "message/global-delivery-status"):
            continue
        try:
            text = part.as_string()
        except Exception:
            continue
        for chunk in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
            fields: dict[str, str] = {}
            for name, key in (("Final-Recipient", "recipient"), ("Original-Recipient", "original"),
                              ("Action", "action"), ("Status", "status"), ("Diagnostic-Code", "diagnostic")):
                if m := re.search(rf"^{name}:\s*(.+(?:\n[ \t].+)*)", chunk, re.I | re.M):
                    fields[key] = " ".join(m.group(1).split())
            if "action" in fields or "status" in fields:
                recipient = fields.get("recipient") or fields.get("original") or ""
                fields["recipient"] = recipient.partition(";")[2].strip().strip("<>").lower() if ";" in recipient \
                    else recipient.strip("<>").lower()
                blocks.append(fields)
    return blocks


def _original_ids(msg: Message, own_id: str) -> tuple[str, ...]:
    ids: list[str] = []
    for part in msg.walk():
        if part.get_content_type() in ("message/rfc822", "text/rfc822-headers", "message/global",
                                       "message/global-headers"):
            try:
                text = part.as_string()
            except Exception:
                continue
            ids += re.findall(r"^Message-ID:\s*(<[^<>\s]+>)", text, re.I | re.M)
    if not ids:  # a bounce that quotes the email in its text
        ids = re.findall(r"^[ \t]*Message-ID:[ \t]*(<[^<>\s]+>)", _all_text(msg), re.I | re.M)
    return tuple(dict.fromkeys(i for i in ids if i != own_id))


# A permanent failure that is about the address, whatever its status code: Microsoft 365's "550 5.4.1 Recipient
# address rejected: Access denied" (no such recipient), a domain that doesn't exist (5.4.4), and RESOLVER.RST.* (a
# Microsoft 365 group or mailbox that takes no mail from outside, a setting of the recipient's, not spam).
_ADDRESS_GONE = re.compile(r"recipient address rejected|recipientnotfound|resolver\.adr\.|"
                           r"host or domain name not found|nonexistentdomain|domain (?:not found|does not exist)", re.I)
_OUTSIDERS_REFUSED = re.compile(r"resolver\.rst\.", re.I)


def address_refused(status: str, words: str) -> bool:
    """A permanent failure that means the address can't get our email (see _ADDRESS_GONE)."""
    return bool(_OUTSIDERS_REFUSED.search(words)) or (status.startswith("5.4.") and bool(_ADDRESS_GONE.search(words)))


def _bounce_kind(status: str, words: str) -> str:
    """bounce (the address doesn't exist: 5.1.x, 5.2.1, address_refused), policy_reject (5.7.x, or spam and block
    words: the sender's reputation, so the address stays) or soft_bounce (delayed, mailbox full 5.2.2, anything
    temporary)."""
    if address_refused(status, words):
        return BOUNCE
    if status.startswith("5.7."):
        return POLICY_REJECT
    if _hard_bounce_status(status) or (status.startswith("5.") and _NO_SUCH_USER.search(words)):
        return BOUNCE
    if status.startswith("5.") and not status.startswith("5.2.") and _POLICY_WORDS.search(words):
        return POLICY_REJECT
    return SOFT_BOUNCE


def _auto_reply(msg: Message, subject: str) -> bool:
    """An automatic answer (RFC 3834, or an out-of-office subject). "Precedence: list" isn't one: a person answering
    from a Google Workspace collaborative inbox sends it (classify_inbound counts it only for mail that refers to
    none of our emails)."""
    auto = _header(msg, "Auto-Submitted").lower()
    precedence = _header(msg, "Precedence").lower()
    return ((bool(auto) and auto != "no") or bool(_header(msg, "X-Autoreply")) or bool(_header(msg, "X-Autorespond"))
            or precedence in ("auto_reply", "bulk", "junk") or bool(_AUTO_SUBJECT.match(subject)))


def _block_status(block: dict[str, str]) -> str:
    """A DSN block's status. A non-specific one (x.0.0: Exim and many relays always write "Status: 5.0.0") gives way
    to the diagnostic's own code ("550 5.1.1 RESOLVER.ADR.RecipNotFound")."""
    status = block.get("status", "")
    told = enhanced_status(block.get("diagnostic", ""))
    if not status or (status.endswith(".0.0") and told and told[0] == status[0]):
        return told or status
    return status


def classify_inbound(raw: bytes, ignore: Iterable[str] = ()) -> Inbound:
    """What an inbox message is: a bounce (hard, soft or a policy rejection), the List-Unsubscribe email, an
    auto-reply (also a newsletter or list mail that answers none of our emails), an opt-out or a reply. `ignore`:
    our own footers, never read as an opt-out. Raises ValueError for a message that can't be read at all."""
    try:
        msg = email.message_from_bytes(raw[:INBOUND_BYTES_MAX], policy=email.policy.default)
    except Exception as exc:
        raise ValueError(f"unreadable message: {type(exc).__name__}") from exc
    from_email = parseaddr(_header(msg, "From"))[1].strip().lower()
    subject = _header(msg, "Subject")
    own_id = (_MESSAGE_IDS.findall(_header(msg, "Message-ID")) or [""])[0]
    references = tuple(dict.fromkeys(_MESSAGE_IDS.findall(f"{_header(msg, 'In-Reply-To', True)} "
                                                          f"{_header(msg, 'References', True)}")))
    base = {"from_email": from_email, "subject": subject, "message_id": own_id, "references": references}

    content_type = msg.get_content_type()
    report = content_type == "multipart/report" and "delivery-status" in str(msg.get_param("report-type") or "")
    system = bool(_SYSTEM_SENDER.match(from_email.partition("@")[0]))
    if report or (system and _BOUNCE_SUBJECT.search(subject)):
        blocks = _dsn_blocks(msg) if report else []
        if blocks:
            chosen = [b for b in blocks if b.get("action", "").lower() in ("failed", "delayed", "")]
            if not chosen:  # delivered, relayed or expanded: nothing failed
                return Inbound(AUTO_REPLY, **base)
            kinds = [(_bounce_kind(_block_status(b), b.get("diagnostic", "")) if b.get("action", "").lower() != "delayed"
                      else SOFT_BOUNCE, b) for b in chosen]
            order = (BOUNCE, POLICY_REJECT, SOFT_BOUNCE)
            kind, block = min(kinds, key=lambda kb: order.index(kb[0]))
            recipients = tuple(dict.fromkeys(b["recipient"] for b in chosen if b.get("recipient")))
            status = _block_status(block)
            diagnostic = server_text(block.get("diagnostic", ""))
        else:  # a bounce that doesn't follow RFC 3464: its words and the addresses in it
            text = _all_text(msg)
            status = next((s for s in (".".join(m) for m in _STATUS.findall(text)) if s.startswith("5.")),
                          enhanced_status(text))
            kind = _bounce_kind(status or ("5.0.0" if _NO_SUCH_USER.search(text) else "4.0.0"), text)
            # The addresses it names, but not ours (the bounce's To) nor Message-IDs, which look like addresses.
            ours = {from_email, *(a.lower() for a in find_addresses(_header(msg, "To", True)))}
            ids = {i.lower() for i in re.findall(r"^[ \t]*Message-ID:[ \t]*<([^<>\s]+)>", text, re.I | re.M)}
            recipients = tuple(a for a in find_addresses(text) if a not in ours and a not in ids)
            diagnostic = server_text(next((line for line in text.splitlines() if status and status in line), ""))
        return Inbound(kind, **base, failed_recipients=recipients, status=status, diagnostic=diagnostic,
                       original_ids=_original_ids(msg, own_id))

    body = _body_text(msg)
    text = strip_quoted(body)[:INBOUND_TEXT_MAX]
    # Whom it may be about: a colleague's forward quotes the email we sent ("To: omar@hotel.ae").
    mentioned = find_addresses(f"{body} {_header(msg, 'To', True)} {_header(msg, 'Cc', True)}", limit=50)
    base = {**base, "mentioned": tuple(a for a in mentioned if a != from_email)}
    if m := _UNSUBSCRIBE_SUBJECT.match(subject):  # the List-Unsubscribe email
        return Inbound(OPT_OUT, **base, text=text, token=m.group(1).lower(), opt_out="unsubscribe")
    # A newsletter from the lead's own address (a hotel's Mailchimp campaign from info@) answers none of our emails:
    # noted, never read for opt-out words. A reply sent through a mailing list (a Google Group, a collaborative inbox)
    # still refers to our email: it is read.
    bulk = not references and bool(_header(msg, "List-Id") or _header(msg, "List-Unsubscribe")
                                   or _header(msg, "Precedence").lower() == "list")
    if _auto_reply(msg, subject) or bulk:
        return Inbound(AUTO_REPLY, **base, text=text)
    sender_name = parseaddr(_header(msg, "From"))[0]
    level, words = read_opt_out(text, subject, ignore, names=[sender_name] if sender_name else [])
    if level == SURE:
        return Inbound(OPT_OUT, **base, text=text, opt_out=words)
    return Inbound(REPLY, **base, text=text, opt_out_hint=words)


def content_key(raw: bytes) -> str:
    """A key for a message without a Message-ID that stays the same wherever it is read again (another folder, or
    after the server renumbered the folder): its sender, recipient, date and subject and the start of its body."""
    head, _, body = raw[:INBOUND_BYTES_MAX].replace(b"\r\n", b"\n").partition(b"\n\n")
    try:
        msg = email.message_from_bytes(head + b"\n\n", policy=email.policy.default)
        facts = "\0".join(_header(msg, name, True) for name in ("From", "To", "Date", "Subject"))
    except Exception:
        facts = ""
    digest = hashlib.sha256(facts.encode("utf-8", "replace") + b"\0" + body[:2048]).hexdigest()
    return f"sha:{digest[:40]}"


def header_facts(headers: Message) -> dict[str, Any]:
    """What the inbox check needs to know about a message before reading it: who sent it, the Message-IDs it
    refers to, and whether it looks like a bounce or the List-Unsubscribe email."""
    from_email = parseaddr(_header(headers, "From"))[1].strip().lower()
    subject = _header(headers, "Subject")
    content_type = _header(headers, "Content-Type").lower()
    refers = f"{_header(headers, 'In-Reply-To', True)} {_header(headers, 'References', True)}"
    return {
        "from_email": from_email,
        "from_domain": domain_of(from_email),
        "references": set(_MESSAGE_IDS.findall(refers)),
        "bounce": "multipart/report" in content_type or bool(_SYSTEM_SENDER.match(from_email.partition("@")[0])),
        "unsubscribe": bool(_UNSUBSCRIBE_SUBJECT.match(subject)),
        "message_id": (_MESSAGE_IDS.findall(_header(headers, "Message-ID")) or [""])[0],
    }
