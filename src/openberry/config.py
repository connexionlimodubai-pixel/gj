"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _dotenv_value(raw: str) -> str:
    """The value part of a KEY=value line.

    Quoted values are kept as written (`"a # b"`). In an unquoted value a `#` after whitespace
    starts a comment (`myname  # note` -> `myname`), as in docker compose; a `#` inside a word
    stays (`pa#ss`).
    """
    value = raw.strip()
    if value[:1] in {'"', "'"} and (end := value.find(value[0], 1)) > 0:
        return value[1:end]
    return re.split(r"\s#", raw, maxsplit=1)[0].strip().strip('"').strip("'")


def read_dotenv(path: Path) -> dict[str, str]:
    """KEY -> value of a .env file ({} when it is missing or unreadable). The first line of a key wins."""
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), _dotenv_value(value))
    return values


# Variables a .env file put into os.environ, and that file (resolved). Anything else in os.environ came from the
# real environment, which always wins.
_FROM_FILE: dict[str, Path] = {}
# The computer's time zone (TZ=Asia/Dubai): in a .env file it sets the clock of the email sending hours, which in
# Docker or on a server is UTC otherwise.
TIME_ZONE_VAR = "TZ"


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):  # a symlink loop, an unreadable folder
        return path.expanduser().absolute()


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win.

    An empty value doesn't count for the settings the dashboard can save (DASHBOARD_SETTINGS): docker compose passes
    `KEY=` lines of its env_file on as empty variables, which would otherwise hide a key saved in the dashboard.
    """
    for key, value in read_dotenv(path).items():
        current = os.environ.get(key)
        if current is None or (key in DASHBOARD_SETTINGS and not current.strip() and value.strip()):
            os.environ[key] = value
            _FROM_FILE[key] = _resolved(path)
            if key == TIME_ZONE_VAR and hasattr(time, "tzset"):
                time.tzset()  # the email sending hours follow this clock (POSIX; Windows has its own setting)


# Fixed per-user home so the dashboard and Claude Desktop (which starts `openberry mcp` from an
# unpredictable working directory) always share the same database and .env file.
OPENBERRY_HOME = Path(os.environ.get("OPENBERRY_HOME", "~/.openberry")).expanduser()
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# Google Maps searches a month: Google's free tier is 1,000, so a margin is kept (its month starts in Pacific time).
DEFAULT_GOOGLE_PLACES_MONTHLY_LIMIT = 900
# Written by the desktop app (desktop.py) while it runs: {"url": ..., "pid": ..., "version": ...}.
DESKTOP_STATE_FILE = "desktop.json"


def desktop_url(home: Path | None = None) -> str:
    """The dashboard URL of the running desktop app (OPENBERRY_HOME/desktop.json), or "" if there is none.

    `openberry mcp`, which Claude Desktop starts, uses it for its dashboard links when
    OPENBERRY_BASE_URL is not set: the app may have picked another port than 8000. A file left
    behind by a crash or a force quit is ignored: no app holds OPENBERRY_HOME/desktop.lock then.
    """
    home = home or OPENBERRY_HOME
    try:
        data = json.loads((home / DESKTOP_STATE_FILE).read_text(encoding="utf-8"))
        url = data.get("url") if isinstance(data, dict) else None
        parts = urlsplit(url) if isinstance(url, str) else None
    except (OSError, ValueError):  # unreadable, not JSON, or a malformed URL
        return ""
    if parts is None or parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    from .desktop import app_is_running  # desktop.py imports this module

    return url.strip().rstrip("/") if app_is_running(home) else ""


def current_base_url(settings: Settings) -> str:
    """The dashboard address for links, checked each time it is needed.

    Without OPENBERRY_BASE_URL (environment or .env) it is the running desktop app's address: a
    long-running `openberry mcp` (Claude Desktop starts it with Claude, maybe before the app) then
    links to the port the app is using now. Otherwise, and when no app runs, settings.base_url.
    """
    if not os.environ.get("OPENBERRY_BASE_URL") and (url := desktop_url()):
        return url
    return settings.base_url.rstrip("/")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: OPENBERRY_HOME / "openberry.db")
    # Public URL of the dashboard: links in MCP replies, an allowed Host name, and Secure
    # session cookies when it is https://.
    base_url: str = DEFAULT_BASE_URL
    # Dashboard login. Empty = no login (fine on localhost, NOT for a public server).
    password: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    # Bearer token for the JSON API and the HTTP MCP endpoint. Empty = disabled when a
    # password is set, open when no password is set (local mode).
    api_token: str = field(default="", repr=False)
    # Let anyone open /register (agency mode: clients fill in their own details).
    public_registration: bool = False
    # Background scheduler: runs each company's signal scan on its interval and, every tick
    # (seconds, min 30), alerts on people who turned hot outside a scan.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 300
    # Expose the MCP server over Streamable HTTP at /mcp in the web app.
    http_mcp_enabled: bool = True
    # Optional free local LLM (https://ollama.com): the "Local AI (Ollama)" writer on a lead page.
    ollama_url: str = ""
    ollama_model: str = "llama3.1"
    # Optional GitHub token: raises the API limit from 60 to 5000 requests/hour.
    github_token: str = field(default="", repr=False)
    # Optional Reddit API app credentials (Reddit blocks unauthenticated access since 2026;
    # commercial use needs Reddit's agreement). The Reddit source stays off without them.
    reddit_client_id: str = ""
    reddit_client_secret: str = field(default="", repr=False)
    reddit_username: str = ""  # used in the User-Agent Reddit requires: "... (by /u/<username>)"
    # Let RSS feeds point at private/loopback hosts (e.g. a local RSSHub). Keep off when public
    # registration is on or the dashboard is shared.
    allow_private_feeds: bool = False
    # Contact e-mail sent in the User-Agent where APIs require one (SEC EDGAR fair-access policy).
    contact_email: str = ""
    # Optional Google Maps Platform key ("Places API (New)") for the Google Maps businesses source. Saved from the
    # dashboard's API keys page or set here; never shown back, never sent to Claude.
    google_places_key: str = field(default="", repr=False)
    # Most Google Maps searches (Text Search requests, one per page of up to 20 businesses) per calendar month (UTC).
    # Google's free tier is 1,000 a month; 0 turns the searches off.
    google_places_monthly_limit: int = DEFAULT_GOOGLE_PLACES_MONTHLY_LIMIT
    # Why Google refused the saved key when the API keys page last checked it: "<last 4 characters of the key>
    # <reason>", "" when Google accepted it. Not secret; written by the API keys page only.
    google_places_key_status: str = ""
    # The user's own mailbox for email sending (docs/EMAIL_SENDING.md), saved from the dashboard's Mailbox page. Only
    # the dashboard process (openberry serve, the desktop app) sends or reads mail; the passwords are never shown,
    # logged or given to Claude or the JSON API.
    mail_provider: str = ""          # the Mailbox page's preset: zoho, zoho_personal, hostinger, gmail or other
    smtp_host: str = ""
    smtp_port: int = 465             # 465 (TLS from the start) or 587 (STARTTLS); nothing else is used
    smtp_username: str = ""          # usually the full email address
    smtp_password: str = field(default="", repr=False)
    smtp_from_email: str = ""        # the From address; the username when empty
    smtp_from_name: str = ""
    imap_host: str = ""
    imap_port: int = 993             # TLS only
    imap_username: str = ""          # the SMTP username when empty
    imap_password: str = field(default="", repr=False)  # the SMTP password when empty
    imap_save_sent: bool = True      # put a copy of each sent email in the mailbox's Sent folder
    user_agent: str = "OpenBerry/0.1 (+https://github.com/connexionlimodubai-pixel/gj)"
    http_timeout: float = 20.0
    # Extra Host names accepted by the HTTP MCP endpoint (without an API token) and, in local
    # mode, the dashboard (besides localhost and base_url's host; the dashboard also accepts
    # any IP address, /mcp does not). "*" disables the check.
    allowed_hosts: list[str] = field(default_factory=list)
    # `openberry serve` on an address other computers can reach refuses to start without a password (start_check),
    # unless this is set: for a login that something in front of OpenBerry asks for.
    allow_no_password: bool = False
    # Reverse proxies whose X-Forwarded-For / X-Forwarded-Proto headers `openberry serve` trusts (uvicorn's
    # forwarded_allow_ips): IP addresses or networks, "*" for any. Empty: uvicorn's default (127.0.0.1).
    trusted_proxies: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        if env_file := os.environ.get("OPENBERRY_ENV_FILE"):
            _load_dotenv(Path(env_file).expanduser())
        else:
            _load_dotenv(Path(".env"))
            _load_dotenv(OPENBERRY_HOME / ".env")
        db = os.environ.get("OPENBERRY_DB")
        s = cls(
            db_path=Path(db).expanduser().resolve() if db else OPENBERRY_HOME / "openberry.db",
            # Unset: the running desktop app's address, else the default `openberry serve` address.
            base_url=(os.environ.get("OPENBERRY_BASE_URL") or desktop_url() or DEFAULT_BASE_URL).rstrip("/"),
            password=os.environ.get("OPENBERRY_PASSWORD", ""),
            secret_key=os.environ.get("OPENBERRY_SECRET_KEY", ""),
            api_token=os.environ.get("OPENBERRY_API_TOKEN", ""),
            public_registration=_bool("OPENBERRY_PUBLIC_REGISTRATION", False),
            scheduler_enabled=_bool("OPENBERRY_SCHEDULER", True),
            scheduler_tick_seconds=_int("OPENBERRY_SCHEDULER_TICK_SECONDS", 300),
            http_mcp_enabled=_bool("OPENBERRY_HTTP_MCP", True),
            ollama_url=os.environ.get("OPENBERRY_OLLAMA_URL", "").rstrip("/"),
            ollama_model=os.environ.get("OPENBERRY_OLLAMA_MODEL", "llama3.1"),
            github_token=os.environ.get("GITHUB_TOKEN", os.environ.get("OPENBERRY_GITHUB_TOKEN", "")),
            reddit_client_id=os.environ.get("REDDIT_CLIENT_ID", ""),
            reddit_client_secret=os.environ.get("REDDIT_CLIENT_SECRET", ""),
            reddit_username=os.environ.get("REDDIT_USERNAME", ""),
            allow_private_feeds=_bool("OPENBERRY_ALLOW_PRIVATE_FEEDS", False),
            contact_email=os.environ.get("OPENBERRY_CONTACT_EMAIL", ""),
            google_places_key=os.environ.get("OPENBERRY_GOOGLE_PLACES_KEY", "").strip(),
            google_places_monthly_limit=max(0, _int("OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT",
                                                    DEFAULT_GOOGLE_PLACES_MONTHLY_LIMIT)),
            google_places_key_status=os.environ.get("OPENBERRY_GOOGLE_PLACES_KEY_STATUS", "").strip(),
            allowed_hosts=[h.strip() for h in os.environ.get("OPENBERRY_ALLOWED_HOSTS", "").split(",") if h.strip()],
            allow_no_password=_bool("OPENBERRY_ALLOW_NO_PASSWORD", False),
            trusted_proxies=[h.strip() for h in os.environ.get("OPENBERRY_TRUSTED_PROXIES", "").split(",")
                             if h.strip()],
        )
        for name in MAILBOX_SETTINGS:  # parsed as apply_setting parses them
            spec = DASHBOARD_SETTINGS[name]
            setattr(s, spec.attr, _parsed(spec, os.environ.get(name)))
        if not s.secret_key:
            # Sessions won't survive restarts without a fixed key; that's acceptable locally.
            s.secret_key = secrets.token_hex(32)
        _mark_settings_file_seen()
        return s

    def mailbox_address(self) -> str:
        """The From address of the connected mailbox (lower case), or "" while it isn't fully set up: email sending
        needs the SMTP server, username and password, and the IMAP server for replies, opt-outs and bounces."""
        if not (self.smtp_host and self.smtp_username and self.smtp_password and self.imap_host):
            return ""
        return (self.smtp_from_email or self.smtp_username).strip().lower()

    def mailbox_key(self) -> str:
        """The account the user signs in with (the SMTP username, lower case), or "" while no mailbox is connected.
        The mailbox's state (pauses, failures, where the inbox check stopped) is kept under it, not under the From
        address, so changing the From address (an alias) never loses unread replies or lifts a provider's block."""
        return self.smtp_username.strip().lower() if self.mailbox_address() else ""


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def set_settings(settings: Settings) -> None:
    """Override settings (used by tests and the CLI)."""
    global _settings
    _settings = settings


# --------------------------------------------------------------------------------------
# Settings the dashboard can save (envfile.save_settings writes them to settings_file())
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DashboardSetting:
    attr: str                       # Settings attribute
    default: Any
    secret: bool                    # never shown back; the dashboard says "ending in 1234" at most
    parse: Callable[[str], Any]     # raw .env text -> attribute value; ValueError -> default
    strip: bool = True              # False: saved exactly as typed (a password may start or end with a space)


def _limit(raw: str) -> int:
    return max(0, int(raw.strip()))


def _port(*allowed: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        port = int(raw.strip())
        if port not in allowed:
            raise ValueError(f"port {port} is not one of {allowed}")
        return port
    return parse


def _flag(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _host(raw: str) -> str:
    return raw.strip().lower()


def _exact(raw: str) -> str:
    return raw


def _parsed(spec: DashboardSetting, raw: str | None) -> Any:
    if raw is None or not raw.strip():
        return spec.default
    try:
        return spec.parse(raw)
    except ValueError:
        return spec.default


# Only these names can be written from the dashboard: the Google Maps key (API keys page) and the mailbox (Mailbox
# page).
DASHBOARD_SETTINGS: dict[str, DashboardSetting] = {
    "OPENBERRY_GOOGLE_PLACES_KEY": DashboardSetting("google_places_key", "", True, str.strip),
    "OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT": DashboardSetting(
        "google_places_monthly_limit", DEFAULT_GOOGLE_PLACES_MONTHLY_LIMIT, False, _limit),
    "OPENBERRY_GOOGLE_PLACES_KEY_STATUS": DashboardSetting("google_places_key_status", "", False, str.strip),
    "OPENBERRY_MAIL_PROVIDER": DashboardSetting("mail_provider", "", False, str.strip),
    "OPENBERRY_SMTP_HOST": DashboardSetting("smtp_host", "", False, _host),
    "OPENBERRY_SMTP_PORT": DashboardSetting("smtp_port", 465, False, _port(465, 587)),
    "OPENBERRY_SMTP_USERNAME": DashboardSetting("smtp_username", "", False, str.strip),
    "OPENBERRY_SMTP_PASSWORD": DashboardSetting("smtp_password", "", True, _exact, strip=False),
    "OPENBERRY_SMTP_FROM_EMAIL": DashboardSetting("smtp_from_email", "", False, str.strip),
    "OPENBERRY_SMTP_FROM_NAME": DashboardSetting("smtp_from_name", "", False, str.strip),
    "OPENBERRY_IMAP_HOST": DashboardSetting("imap_host", "", False, _host),
    "OPENBERRY_IMAP_PORT": DashboardSetting("imap_port", 993, False, _port(993)),
    "OPENBERRY_IMAP_USERNAME": DashboardSetting("imap_username", "", False, str.strip),
    "OPENBERRY_IMAP_PASSWORD": DashboardSetting("imap_password", "", True, _exact, strip=False),
    "OPENBERRY_IMAP_SAVE_SENT": DashboardSetting("imap_save_sent", True, False, _flag),
}
# The mailbox's settings (the Mailbox page), read by Settings.from_env through the same parsers.
MAILBOX_SETTINGS = tuple(name for name in DASHBOARD_SETTINGS
                         if name.startswith(("OPENBERRY_SMTP_", "OPENBERRY_IMAP_", "OPENBERRY_MAIL_")))

# What settings_file() looked like when this process last read it: (inode, mtime_ns, size), None before the first
# look. os.replace gives every save a new inode, so two saves within one clock tick still differ.
_settings_file_seen: tuple[int, int, int] | None = None


def settings_file() -> Path:
    """The .env file the dashboard writes: OPENBERRY_ENV_FILE when set (it is then the only file read),
    else OPENBERRY_HOME/.env (~/.openberry/.env, the data folder)."""
    if env_file := os.environ.get("OPENBERRY_ENV_FILE"):
        return Path(env_file).expanduser()
    return OPENBERRY_HOME / ".env"


def _file_signature(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
    except OSError:
        return (-1, -1, -1)
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _mark_settings_file_seen() -> None:
    global _settings_file_seen
    _settings_file_seen = _file_signature(settings_file())


def setting_source(name: str) -> tuple[str, Path | None]:
    """Where a setting's value comes from.

    ("environment", None): a real environment variable (it wins, the dashboard can't change it);
    ("file", path): loaded from (or saved to) that .env file; ("", None): not set, or set to an empty value.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        return "", None
    if name in _FROM_FILE:
        return "file", _FROM_FILE[name]
    return "environment", None


def apply_setting(name: str, value: str | None, path: Path, settings: Settings | None = None) -> None:
    """Make a value saved to `path` live in this process: os.environ, _FROM_FILE and the cached Settings.

    The Settings object is changed in place, so app.state.settings (the same object) sees it, and every other
    field (secret_key included: logins survive) stays as it is. None removes it (the attribute goes back to its
    default).
    """
    spec = DASHBOARD_SETTINGS[name]
    settings = settings or get_settings()
    if value is None or not value.strip():
        os.environ.pop(name, None)
        _FROM_FILE.pop(name, None)
        setattr(settings, spec.attr, spec.default)
        return
    os.environ[name] = value
    _FROM_FILE[name] = _resolved(path)
    setattr(settings, spec.attr, _parsed(spec, value))


def refresh_saved_settings(settings: Settings | None = None) -> bool:
    """Apply dashboard settings another process saved to settings_file() since this process last looked.

    Cheap: one stat() call; the file is parsed only when it changed. Only DASHBOARD_SETTINGS names change, and only
    when they don't come from the real environment or from another .env file that is read before this one (./.env).
    Returns True when a value changed. Called at the start of each scan, by the collector overview, the API keys
    page and the Google Maps usage summary, so the long-running `openberry mcp` that Claude Desktop starts and a
    second `openberry serve` see a key saved in the dashboard without a restart. get_settings() never does this by
    itself: a value changing in the middle of a request is not wanted.
    """
    global _settings_file_seen
    path = settings_file()
    signature = _file_signature(path)
    if signature == _settings_file_seen:
        return False
    _settings_file_seen = signature
    values = read_dotenv(path)
    target = _resolved(path)
    changed = False
    for name in DASHBOARD_SETTINGS:
        source, where = setting_source(name)
        if source == "environment" or (source == "file" and where != target):
            continue
        new = values.get(name)
        new = new if new is not None and new.strip() else None
        current = os.environ.get(name) if source == "file" else None
        if new != current:
            apply_setting(name, new, path, settings)
            changed = True
    return changed


# --------------------------------------------------------------------------------------
# Starting a server other computers can reach (`openberry serve --host 0.0.0.0`)
# --------------------------------------------------------------------------------------

MIN_PASSWORD_CHARS = 12
MIN_SECRET_CHARS = 32
SECRET_COMMAND = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'
SHORT_TOKEN_WARNING = (f"OPENBERRY_API_TOKEN is shorter than {MIN_SECRET_CHARS} characters, which makes it easier to "
                       f"guess. To make one: {SECRET_COMMAND}")


def is_loopback(host: str) -> bool:
    """True for addresses only this computer can reach: localhost, 127.0.0.0/8 and ::1."""
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


@dataclass
class StartCheck:
    refusals: list[str] = field(default_factory=list)  # the server must not start
    warnings: list[str] = field(default_factory=list)


def start_check(settings: Settings, host: str, environ: Any = None) -> StartCheck:
    """What is unsafe about serving the dashboard on `host` with these settings. Nothing on a loopback address: there
    only this computer can open it, so `openberry serve` and the desktop app work as before, without a password."""
    environ = os.environ if environ is None else environ
    check = StartCheck()
    if is_loopback(host):
        return check
    make = f"To make one: {SECRET_COMMAND}"
    password = settings.password
    if not password and not settings.allow_no_password:
        check.refusals.append(
            f"OpenBerry has no password, and on {host} other computers can open it: anyone who reaches it could read "
            f"and change your data. Set OPENBERRY_PASSWORD ({MIN_PASSWORD_CHARS} characters or more), or serve on "
            "127.0.0.1. If something in front of OpenBerry already asks for a login, set "
            "OPENBERRY_ALLOW_NO_PASSWORD=true.")
    elif password and len(password) < MIN_PASSWORD_CHARS:
        check.refusals.append(
            f"OPENBERRY_PASSWORD is too short: use at least {MIN_PASSWORD_CHARS} characters when other computers can "
            f"open OpenBerry (on {host}). {make}")
    if password:
        secret = environ.get("OPENBERRY_SECRET_KEY", "")
        if not secret:
            check.warnings.append(f"OPENBERRY_SECRET_KEY is not set, so every restart logs everyone out. {make}")
        elif len(secret) < MIN_SECRET_CHARS:
            check.warnings.append(f"OPENBERRY_SECRET_KEY is shorter than {MIN_SECRET_CHARS} characters: someone who "
                                  f"guesses it can make their own login. {make}")
    token = settings.api_token
    if not token:
        check.warnings.append(
            "OPENBERRY_API_TOKEN is not set, so the JSON API and /mcp are off." if password else
            "OPENBERRY_API_TOKEN is not set, so anyone who reaches OpenBerry can use its JSON API and /mcp. "
            + make)
    elif len(token) < MIN_SECRET_CHARS:
        check.warnings.append(SHORT_TOKEN_WARNING)
    base = urlsplit(settings.base_url)
    if base.scheme == "http" and base.hostname and not is_loopback(base.hostname):
        check.warnings.append(
            f"OPENBERRY_BASE_URL is {settings.base_url}: over http:// the password and the login cookie cross the "
            "network unencrypted. Put OpenBerry behind an HTTPS proxy and set OPENBERRY_BASE_URL to its https:// "
            "address.")
    return check


def mcp_start_check(settings: Settings, host: str) -> StartCheck:
    """What is unsafe about `openberry mcp --http` on `host`. Nothing on a loopback address. Elsewhere it needs
    OPENBERRY_API_TOKEN, even with OPENBERRY_ALLOW_NO_PASSWORD: its tools read, change and delete all the data, and
    without a token only the Host check guards it, which stops DNS rebinding but not another computer (it can send
    any Host)."""
    check = StartCheck()
    if is_loopback(host):
        return check
    if not settings.api_token:
        check.refusals.append(
            f"The MCP server has no API token, and on {host} other computers can reach it. Its tools read, change and "
            "delete all your data, so it needs a token there, even with OPENBERRY_ALLOW_NO_PASSWORD=true. Set "
            f"OPENBERRY_API_TOKEN ({MIN_SECRET_CHARS} characters or more), or use --host 127.0.0.1. To make one: "
            f"{SECRET_COMMAND}")
    elif len(settings.api_token) < MIN_SECRET_CHARS:
        check.warnings.append(SHORT_TOKEN_WARNING)
    return check


# --------------------------------------------------------------------------------------
# Private files: the data folder holds personal data (leads, inbox replies) and the mailbox password
# --------------------------------------------------------------------------------------

PRIVATE_UMASK = 0o077


def private_files() -> None:
    """Files and folders this process creates are for this user only (Linux, macOS, Docker; Windows ignores it).

    The data folder (OPENBERRY_HOME) and the .env file the dashboard saves (settings_file()) are made private too when
    they exist and this user owns them: an older version, or a text editor, may have left them readable by everyone.
    """
    os.umask(PRIVATE_UMASK)
    if os.name != "nt":
        if not _home_or_above(OPENBERRY_HOME):  # OPENBERRY_HOME=~ would otherwise lock other accounts out of it
            _owner_only(OPENBERRY_HOME, 0o700, folder=True)
        _owner_only(settings_file(), 0o600, folder=False)


def _home_or_above(path: Path) -> bool:
    try:
        home = Path.home().resolve()
        return path.resolve() in {home, *home.parents}
    except (OSError, RuntimeError):  # no home folder to compare with
        return True


def _owner_only(path: Path, mode: int, folder: bool) -> None:
    """chmod `path` to `mode` when it is a folder (or a regular file) this user owns. A symlink is followed, as when
    the dashboard saves the .env file. A device, FIFO or socket is left alone, and so is a shared folder such as /tmp
    (it has the sticky bit): as root, OPENBERRY_ENV_FILE=/dev/null or OPENBERRY_HOME=/tmp would otherwise lock every
    other account out of it. Errors are ignored: OpenBerry still starts."""
    try:
        st = path.stat()
        kind_ok = (stat.S_ISDIR(st.st_mode) and not st.st_mode & stat.S_ISVTX) if folder else stat.S_ISREG(st.st_mode)
        if st.st_uid == os.getuid() and kind_ok and stat.S_IMODE(st.st_mode) != mode:
            path.chmod(mode)
    except OSError:
        pass


def private_dir(path: Path) -> Path:
    """Create `path`, and the parents it is missing, as folders only this user can open (0700). A folder that already
    exists keeps its permissions: it may be one the user shares on purpose."""
    path = Path(path)
    missing: list[Path] = []
    folder = path
    while not folder.exists() and folder.parent != folder:
        missing.append(folder)
        folder = folder.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():  # a file in the way: the same error as mkdir gives
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def make_private(*paths: Path) -> None:
    """chmod 0600 the files that exist among `paths` (the database and its -wal and -shm). Errors are ignored: a file
    another user owns, or a file system without permissions."""
    for path in paths:
        try:
            if path.is_file():
                path.chmod(0o600)
        except OSError:
            pass
