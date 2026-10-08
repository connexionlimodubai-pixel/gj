"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


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
    db_path: Path = field(default_factory=lambda: Path("data/openberry.db"))
    # Public URL of the dashboard, used for links in MCP replies and alerts.
    base_url: str = "http://127.0.0.1:8000"
    # Dashboard login. Empty = no login (fine on localhost, NOT for a public server).
    password: str = ""
    secret_key: str = ""
    # Bearer token for the JSON API and the HTTP MCP endpoint. Empty = disabled when a
    # password is set, open when no password is set (local mode).
    api_token: str = ""
    # Let anyone open /register (agency mode: clients fill in their own details).
    public_registration: bool = False
    # Background scheduler that runs signal scans every company's scan interval.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 300
    # Expose the MCP server over Streamable HTTP at /mcp in the web app.
    http_mcp_enabled: bool = True
    # Optional free local LLM (https://ollama.com) used by the dashboard's "Draft with AI".
    ollama_url: str = ""
    ollama_model: str = "llama3.1"
    # Optional GitHub token: raises the API limit from 60 to 5000 requests/hour.
    github_token: str = ""
    user_agent: str = "OpenBerry/0.1 (+https://github.com/connexionlimodubai-pixel/gj)"
    http_timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(Path(os.environ.get("OPENBERRY_ENV_FILE", ".env")))
        s = cls(
            db_path=Path(os.environ.get("OPENBERRY_DB", "data/openberry.db")).expanduser(),
            base_url=os.environ.get("OPENBERRY_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
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
