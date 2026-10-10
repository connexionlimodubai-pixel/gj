"""Save settings from the dashboard into the data folder's .env file (the one config.py reads).

Only names in config.DASHBOARD_SETTINGS can be written, and only from the dashboard (the JSON API and the MCP
server never read or set them). The file is replaced atomically and is readable by this user only (0600 where
the OS supports it; on Windows the user profile folder is private already). Every other line and comment of the
file is kept as it was. Secrets are never shown again: setting_state() gives "ending in 1234" at most.

Two dashboards saving at the same moment: the last write wins for the whole file. Only the dashboard writes it.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import config

SECRET_HINT_MIN_CHARS = 12  # shorter secrets get no hint: four characters would give too much away
REPLACE_ATTEMPTS = 3        # Windows refuses to replace a file another program has open; it is usually brief
_lock = threading.Lock()


class SettingNotSaved(ValueError):
    """The value can't be saved here: not a dashboard setting, a value a .env file can't hold, or the name is set
    somewhere OpenBerry reads first (the environment, or another .env file). The message says what to do."""


@dataclass(frozen=True)
class SettingState:
    name: str
    is_set: bool
    source: str  # "saved" (settings_file()), "file" (another .env file), "environment", or ""
    where: str   # that file's path, "" otherwise
    hint: str    # secret: "ending in 4f2c" (only for long values, else ""); not secret: the value


def secret_hint(value: str) -> str:
    value = (value or "").strip()
    return f"ending in {value[-4:]}" if len(value) >= SECRET_HINT_MIN_CHARS else ""


def setting_state(name: str) -> SettingState:
    """What the dashboard may say about a setting. Never the value of a secret."""
    spec = config.DASHBOARD_SETTINGS[name]
    source, where = config.setting_source(name)
    value = os.environ.get(name, "") if source else ""
    if source == "file" and where == config._resolved(config.settings_file()):
        source = "saved"
    hint = secret_hint(value) if spec.secret else value.strip()
    return SettingState(name=name, is_set=bool(source), source=source,
                        where=str(where) if source == "file" and where else "", hint=hint)


def format_line(name: str, value: str) -> str:
    """NAME="value", or NAME='value' when the value contains a double quote (config._dotenv_value reads both)."""
    return f"{name}='{value}'" if '"' in value else f'{name}="{value}"'


def _check(name: str, value: str | None) -> str | None:
    if name not in config.DASHBOARD_SETTINGS:
        raise SettingNotSaved(f"{name} can't be changed from the dashboard.")
    source, where = config.setting_source(name)
    if source == "environment":
        raise SettingNotSaved(f"{name} is set in the server's environment. Change it there.")
    if source == "file" and where != config._resolved(config.settings_file()):
        raise SettingNotSaved(f"{name} is set in {where}, which OpenBerry reads first. Change it there.")
    value = (value or "").strip()
    if not value:
        return None
    if any(ch in value for ch in "\n\r\0") or ('"' in value and "'" in value):
        raise SettingNotSaved("Use a value without line breaks or quotes.")
    return value


def _merged_lines(lines: list[str], values: Mapping[str, str | None]) -> list[str]:
    """Replace the first active NAME= line of each name, drop its later duplicates, append the new names."""
    out: list[str] = []
    done: set[str] = set()
    for line in lines:
        name = next((n for n in values if re.match(rf"\s*{re.escape(n)}\s*=", line)), None)
        if name is None:
            out.append(line)
            continue
        if name not in done and values[name] is not None:
            out.append(format_line(name, values[name] or ""))
        done.add(name)
    out.extend(format_line(n, v) for n, v in values.items() if n not in done and v is not None)
    return out


def _write_private(path: Path, text: str) -> None:
    """Replace `path` atomically with a file only this user can read."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        path = path.resolve()  # update the link's target, keep the user's link
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.1)
        if sys.platform != "win32":
            os.chmod(path, 0o600)  # also tightens a .env file that was readable by others
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_settings(changes: Mapping[str, str | None]) -> Path:
    """Write dashboard settings to config.settings_file() and make them live in this process at once.

    None or "" removes a setting. Raises SettingNotSaved (nothing is written) or OSError. Values are never logged.
    """
    values = {name: _check(name, value) for name, value in changes.items()}
    path = config.settings_file()
    with _lock:
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except UnicodeDecodeError as exc:
            raise SettingNotSaved(f"{path} is not a text file. Fix or remove it, then save again.") from exc
        lines = _merged_lines(text.splitlines(), values)
        _write_private(path, "\n".join(lines) + "\n" if lines else "")
        for name, value in values.items():
            config.apply_setting(name, value, path)
        config._mark_settings_file_seen()
    return path
