"""Fake SMTP and IMAP servers for the email sending tests: no test ever reaches a real mail server.

`offline_mail` (a fixture) puts FakeSMTP, FakeSMTPSSL and FakeIMAP in place of smtplib.SMTP, smtplib.SMTP_SSL and
imaplib.IMAP4_SSL, and makes any real socket connection fail the test. Tests drive the fakes through `MailWorld`:
what each SMTP step answers (or raises), the IMAP folders and their messages, and a log of every command.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from openberry import mailer

FIXTURES = Path(__file__).parent / "fixtures" / "email"
OUR_ID = "<5f2b9c1e0a7d4e6b8c3f1a2d9e8b7c6a@acme-mail.example>"  # the Message-ID the .eml fixtures refer to
FROM = "sam@acme-mail.example"
SMTP_HOST = "smtp.acme-mail.example"
IMAP_HOST = "imap.acme-mail.example"


def eml(name: str, **replace: str) -> bytes:
    """A fixture message, with {placeholders} filled in (e.g. to= for the lead's address)."""
    raw = (FIXTURES / name).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:  # malformed.eml: given as it is
        return raw
    for key, value in replace.items():
        text = text.replace("{" + key + "}", value)
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


@dataclass
class Folder:
    uidvalidity: int = 1
    messages: dict[int, bytes] = field(default_factory=dict)
    flags: str = ""

    @property
    def uidnext(self) -> int:
        return max(self.messages, default=0) + 1

    def add(self, raw: bytes) -> int:
        uid = self.uidnext
        self.messages[uid] = raw
        return uid


@dataclass
class MailWorld:
    """Everything the fake servers do and saw."""

    log: list[tuple[Any, ...]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)       # emails the SMTP server accepted
    refused: list[dict[str, Any]] = field(default_factory=list)    # emails it received and refused after DATA
    fail: dict[str, Any] = field(default_factory=dict)             # SMTP step -> exception to raise or (code, text)
    starttls: bool = True                                          # port 587 offers STARTTLS
    folders: dict[str, Folder] = field(default_factory=lambda: {"INBOX": Folder(), "Junk": Folder(flags="\\Junk"),
                                                                 "Sent": Folder(flags="\\Sent")})
    imap_fail: dict[str, BaseException] = field(default_factory=dict)  # IMAP step -> exception
    appended: list[tuple[str, bytes]] = field(default_factory=list)
    imap_logins: list[tuple[str, str]] = field(default_factory=list)
    smtp_logins: list[tuple[str, str]] = field(default_factory=list)

    def smtp_commands(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "smtp"]

    def imap_commands(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "imap"]

    def deliver(self, raw: bytes, folder: str = "INBOX") -> int:
        return self.folders[folder].add(raw)


class _FakeSMTPBase:
    world: MailWorld

    def __init__(self, host: str = "", port: int = 0, local_hostname: str | None = None,
                 timeout: float | None = None, **kwargs: Any) -> None:
        self.host, self.port = host, port
        self.local_hostname, self.timeout = local_hostname, timeout
        self.kwargs = kwargs
        self.tls = False
        self._note("connect", host, port, local_hostname, timeout, type(self).__name__, kwargs.get("context"))
        self._step("connect")

    def _note(self, *entry: Any) -> None:
        self.world.log.append(("smtp", *entry))

    def _step(self, name: str) -> tuple[int, bytes] | None:
        outcome = self.world.fail.get(name)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def ehlo(self, name: str = "") -> tuple[int, bytes]:
        self._note("ehlo")
        self._step("ehlo")
        return 250, b"ok"

    def has_extn(self, name: str) -> bool:
        return name.lower() == "starttls" and self.world.starttls

    def starttls(self, context: Any = None) -> tuple[int, bytes]:
        self._note("starttls", context)
        self._step("starttls")
        self.tls = True
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self._note("login", self.tls)
        self._step("login")
        password.encode("ascii")  # smtplib sends it as ASCII
        self.world.smtp_logins.append((user, password))
        return 235, b"ok"

    def docmd(self, cmd: str, args: str = "") -> tuple[int, bytes]:
        self._note("docmd", cmd)
        return 235, b"ok"

    def mail(self, sender: str, options: Any = ()) -> tuple[int, bytes]:
        self._note("mail", sender)
        return self._step("mail") or (250, b"ok")

    def rcpt(self, recip: str, options: Any = ()) -> tuple[int, bytes]:
        self._note("rcpt", recip)
        self.rcpt_to = recip
        return self._step("rcpt") or (250, b"ok")

    def rset(self) -> tuple[int, bytes]:
        self._note("rset")
        return 250, b"ok"

    def data(self, msg: bytes) -> tuple[int, bytes]:
        # As smtplib does: it raises only when the server refuses to start DATA (no 354, "data_start"). The final
        # answer, once the server has the whole email, is returned, whatever it is (a 554 spam refusal too).
        self._note("data")
        start = self.world.fail.get("data_start")
        if isinstance(start, BaseException):
            raise start
        if start is not None:
            raise smtplib.SMTPDataError(*start)
        outcome = self._step("data")
        if outcome is not None:
            self.world.refused.append({"to": self.rcpt_to, "data": msg, "answer": outcome})
            return outcome
        self.world.sent.append({"to": self.rcpt_to, "data": msg})
        return 250, b"2.0.0 queued"

    def quit(self) -> tuple[int, bytes]:
        self._note("quit")
        return 221, b"bye"

    def close(self) -> None:
        self._note("close")


class FakeIMAP:
    world: MailWorld

    def __init__(self, host: str = "", port: int = 993, ssl_context: Any = None, timeout: float | None = None) -> None:
        self.host, self.port, self.ssl_context, self.timeout = host, port, ssl_context, timeout
        self.selected = ""
        self._note("connect", host, port, timeout)
        self._raise("connect")

    def _note(self, *entry: Any) -> None:
        self.world.log.append(("imap", *entry))

    def _raise(self, name: str) -> None:
        if name in self.world.imap_fail:
            raise self.world.imap_fail[name]

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self._note("LOGIN")
        self._raise("login")
        password.encode("ascii")
        self.world.imap_logins.append((user, password))
        return "OK", [b"logged in"]

    def authenticate(self, mechanism: str, authobject: Any) -> tuple[str, list[bytes]]:
        self._note("AUTHENTICATE", mechanism)
        self.world.imap_logins.append(("authenticate", authobject(b"").decode()))
        return "OK", [b"logged in"]

    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list[bytes]]:
        self._note("LIST")
        lines = [f'(\\HasNoChildren{" " + f.flags if f.flags else ""}) "/" "{name}"'.encode()
                 for name, f in self.world.folders.items()]
        return "OK", lines

    @staticmethod
    def _name(mailbox: str) -> str:
        return mailbox[1:-1] if mailbox.startswith('"') else mailbox

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self._note("EXAMINE" if readonly else "SELECT", self._name(mailbox))
        self._raise("select")
        self.selected = self._name(mailbox)
        return "OK", [str(len(self.world.folders[self.selected].messages)).encode()]

    def status(self, mailbox: str, names: str) -> tuple[str, list[bytes]]:
        self._note("STATUS", self._name(mailbox))
        self._raise("status")
        folder = self.world.folders[self._name(mailbox)]
        return "OK", [f'"{self._name(mailbox)}" (UIDVALIDITY {folder.uidvalidity} UIDNEXT {folder.uidnext})'.encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self._note(f"UID {command}", *args)
        self._raise(f"uid {command.lower()}")
        folder = self.world.folders[self.selected]
        if command == "SEARCH":
            if args[0] == "SINCE":
                uids = sorted(folder.messages)
            else:
                start = int(re.match(r"UID (\d+):\*", args[0]).group(1))
                uids = [u for u in sorted(folder.messages) if u >= start] or ([max(folder.messages)] if folder.messages
                                                                               else [])
            return "OK", [" ".join(str(u) for u in uids).encode()]
        if command == "FETCH":
            wanted = [int(u) for u in args[0].split(",")]
            part = args[1]
            assert "BODY.PEEK[" in part, "the inbox check must never mark mail as read"
            out: list[Any] = []
            for uid in wanted:
                raw = folder.messages.get(uid)
                if raw is None:
                    continue
                if "HEADER" in part:
                    raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                out.append((f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        raise AssertionError(f"UID {command} is not allowed: the inbox check is read-only")

    def append(self, mailbox: str, flags: str, date_time: str, message: bytes) -> tuple[str, list[bytes]]:
        self._note("APPEND", self._name(mailbox), flags)
        self._raise("append")
        self.world.appended.append((self._name(mailbox), message))
        return "OK", [b"appended"]

    def logout(self) -> tuple[str, list[bytes]]:
        self._note("LOGOUT")
        return "BYE", [b""]


def _no_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"a test tried to open a real network connection: {args[:1]}")


@pytest.fixture
def offline_mail(monkeypatch: pytest.MonkeyPatch) -> MailWorld:
    """Fake mail servers in place of smtplib and imaplib, and no real connection possible."""
    world = MailWorld()
    smtp = type("FakeSMTP", (_FakeSMTPBase,), {"world": world})
    smtp_ssl = type("FakeSMTPSSL", (_FakeSMTPBase,), {"world": world})
    imap = type("FakeIMAP4SSL", (FakeIMAP,), {"world": world})
    monkeypatch.setattr(smtplib, "SMTP", smtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", smtp_ssl)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", imap)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    world.smtp_class, world.smtp_ssl_class = smtp, smtp_ssl  # type: ignore[attr-defined]
    return world


def connect_mailbox(settings: Any, *, port: int = 465, provider: str = "zoho", save_sent: bool = True,
                    password: str = "app-password-1") -> None:
    """A connected mailbox, as the Mailbox page saves it (the passwords never reach the database)."""
    settings.mail_provider = provider
    settings.smtp_host, settings.smtp_port = SMTP_HOST, port
    settings.smtp_username, settings.smtp_password = FROM, password
    settings.smtp_from_email, settings.smtp_from_name = FROM, "Sam Rahman"
    settings.imap_host, settings.imap_port = IMAP_HOST, 993
    settings.imap_username, settings.imap_password = "", ""
    settings.imap_save_sent = save_sent


def a_mailbox(**changes: Any) -> mailer.Mailbox:
    base = {"smtp_host": SMTP_HOST, "smtp_port": 465, "username": FROM, "password": "app-password-1",
            "from_email": FROM, "from_name": "Sam Rahman", "imap_host": IMAP_HOST}
    return mailer.Mailbox(**{**base, **changes})
