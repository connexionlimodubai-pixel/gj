"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
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


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), _dotenv_value(value))


# Fixed per-user home so the dashboard and Claude Desktop (which starts `openberry mcp` from an
# unpredictable working directory) always share the same database and .env file.
OPENBERRY_HOME = Path(os.environ.get("OPENBERRY_HOME", "~/.openberry")).expanduser()
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
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
            allowed_hosts=[h.strip() for h in os.environ.get("OPENBERRY_ALLOWED_HOSTS", "").split(",") if h.strip()],
        )
        if not s.secret_key:
            # Sessions won't survive restarts without a fixed key; that's acceptable locally.
            s.secret_key = secrets.token_hex(32)
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
