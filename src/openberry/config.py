"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import json
import os
import re
import secrets
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
    password: str = ""
    secret_key: str = ""
    # Bearer token for the JSON API and the HTTP MCP endpoint. Empty = disabled when a
    # password is set, open when no password is set (local mode).
    api_token: str = ""
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
    github_token: str = ""
    # Optional Reddit API app credentials (Reddit blocks unauthenticated access since 2026;
    # commercial use needs Reddit's agreement). The Reddit source stays off without them.
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
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
    user_agent: str = "OpenBerry/0.1 (+https://github.com/connexionlimodubai-pixel/gj)"
    http_timeout: float = 20.0
    # Extra Host names accepted by the HTTP MCP endpoint (without an API token) and, in local
    # mode, the dashboard (besides localhost and base_url's host; the dashboard also accepts
    # any IP address, /mcp does not). "*" disables the check.
    allowed_hosts: list[str] = field(default_factory=list)

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
        )
        if not s.secret_key:
            # Sessions won't survive restarts without a fixed key; that's acceptable locally.
            s.secret_key = secrets.token_hex(32)
        _mark_settings_file_seen()
        return s


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


def _limit(raw: str) -> int:
    return max(0, int(raw.strip()))


# Only these names can be written from the dashboard. SMTP/IMAP credentials, for example, would be one entry each.
DASHBOARD_SETTINGS: dict[str, DashboardSetting] = {
    "OPENBERRY_GOOGLE_PLACES_KEY": DashboardSetting("google_places_key", "", True, str.strip),
    "OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT": DashboardSetting(
        "google_places_monthly_limit", DEFAULT_GOOGLE_PLACES_MONTHLY_LIMIT, False, _limit),
    "OPENBERRY_GOOGLE_PLACES_KEY_STATUS": DashboardSetting("google_places_key_status", "", False, str.strip),
}

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
    try:
        parsed = spec.parse(value)
    except ValueError:
        parsed = spec.default
    setattr(settings, spec.attr, parsed)


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
