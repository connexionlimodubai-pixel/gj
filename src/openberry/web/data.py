"""The Your data page: where the data lives, Download backup, the automatic backups (backups.py) and how to restore one.

Logged-in dashboard users only (in local mode: this machine). It only reads: a backup is a copy, made with SQLite's
backup API while OpenBerry keeps running. Restoring replaces the data, so it is a command run while OpenBerry is closed
(`openberry restore FILE`), never a button here.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, Response

from .. import backups, config, db
from .auth import require_login
from .pages import DOCKER_CONTAINER, mcp_launch, shell_line
from .ui import render

router = APIRouter(dependencies=[Depends(require_login)], include_in_schema=False)

SQLITE_TYPE = "application/vnd.sqlite3"
DOWNLOAD_PREFIX = ".download-"
STALE_DOWNLOAD_SECONDS = 3600
NO_STORE = {"Cache-Control": "no-store"}


def size_text(size: int) -> str:
    """1.2 MB, 340 KB, 900 bytes."""
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{round(size / 1_000)} KB"
    return f"{size} bytes"


def restore_command(example: str) -> dict[str, Any]:
    """The `openberry restore` line for this install (the packaged app's openberry-cli, uv for a clone, else this
    Python), or Docker's steps."""
    db_file = config.get_settings().db_path
    launch = mcp_launch(str(db_file.resolve()))
    if launch["docker"]:  # docker compose, from the folder with docker-compose.yml
        return {"docker": True, "line": "", "db_env": "", "steps": [
            f"docker compose stop {DOCKER_CONTAINER}",
            f"docker compose run --rm {DOCKER_CONTAINER} openberry restore /data/{backups.FOLDER}/{Path(example).name}",
            f"docker compose start {DOCKER_CONTAINER}"]}
    words = [launch["command"], *launch["args"][:-1], "restore", example]  # args end with "mcp"
    default = (config.OPENBERRY_HOME / "openberry.db").resolve()
    return {"docker": False, "line": shell_line(words),
            "db_env": "" if db_file.resolve() == default else str(db_file.resolve())}


def data_context() -> dict[str, Any]:
    settings = config.get_settings()
    db_file = settings.db_path
    snaps = backups.list_snapshots(db_file)
    try:
        db_size = db_file.stat().st_size + sum(
            p.stat().st_size for p in (db_file.with_name(db_file.name + "-wal"),) if p.exists())
    except OSError:
        db_size = 0
    example = str(snaps[0].path) if snaps else backups.download_name()
    return {
        "active": "data", "title": "Your data",
        "data_folder": str(db_file.resolve().parent), "db_file": db_file.name, "db_size": size_text(db_size),
        "env_path": str(config.settings_file()), "backups_folder": str(backups.folder(db_file).resolve()),
        "snapshots": [{"name": s.name, "label": s.label, "created": s.created, "size": size_text(s.size)}
                      for s in snaps],
        "daily_on": settings.scheduler_enabled, "keep": backups.KEEP, "daily_days": backups.DAILY_DAYS,
        "daily_weeks": backups.DAILY_WEEKS, "download_name": backups.download_name(),
        "restore": restore_command(example), "example": example,
    }


@router.get("/data")
def data_page(request: Request) -> Response:
    return render(request, "data.html", data_context(), headers=NO_STORE)


def _remove_stale_downloads(folder: Path) -> None:
    """Delete copies a download left behind (the app closed while it was being sent)."""
    for path in folder.glob(f"{DOWNLOAD_PREFIX}*.db"):
        with contextlib.suppress(OSError):
            if time.time() - path.stat().st_mtime > STALE_DOWNLOAD_SECONDS:
                path.unlink()


def _fresh_copy() -> Path:
    db_file = db.prepare()
    folder = db_file.resolve().parent
    _remove_stale_downloads(folder)
    fd, name = tempfile.mkstemp(prefix=DOWNLOAD_PREFIX, suffix=".db", dir=folder)
    os.close(fd)
    path = Path(name)
    try:
        db.backup_to(path, db_file)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return path


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


@router.get("/data/backup")
async def download_backup(request: Request) -> Response:
    """A fresh, consistent copy of the whole database, as openberry-backup-YYYY-MM-DD.db."""
    path = await run_in_threadpool(_fresh_copy)
    return FileResponse(path, media_type=SQLITE_TYPE, filename=backups.download_name(), headers=NO_STORE,
                        background=BackgroundTask(_unlink, path))


@router.get("/data/backups/{name}")
def download_snapshot(request: Request, name: str) -> Response:
    """One of the automatic backups, by its exact file name (nothing outside the backups folder)."""
    snap = backups.find(name)
    if snap is None:
        from ..repo import NotFound

        raise NotFound(f"backup {name}")
    return FileResponse(snap.path, media_type=SQLITE_TYPE, filename=snap.name, headers=NO_STORE)
