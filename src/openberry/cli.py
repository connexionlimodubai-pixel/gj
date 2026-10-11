"""Command line: `openberry serve | desktop | mcp | scan | rescore | init-db | backup | restore | remove-sample`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__


def serve_options(settings: Any, host: str, port: int) -> dict[str, Any]:
    """uvicorn.run's options for `openberry serve`."""
    return {
        "host": host, "port": port, "log_level": "info",
        # X-Forwarded-For/-Proto from these proxies only (None: uvicorn's default, 127.0.0.1). Behind a proxy
        # container every visitor would otherwise have the proxy's address and share one login limit.
        "proxy_headers": True, "forwarded_allow_ips": settings.trusted_proxies or None,
        "access_log": False,  # its lines would log every lead search's query string
    }


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .config import OPENBERRY_HOME, get_settings
    from .desktop import serve_lock
    from .web.app import UnsafeStart, create_app

    settings = get_settings()
    try:
        app = create_app(host=args.host)  # refuses a host other computers reach without a (long enough) password
    except UnsafeStart as exc:
        sys.exit(f"openberry serve: {exc}")
    lock = serve_lock(OPENBERRY_HOME)
    lock.acquire()  # held while it serves: `openberry restore` refuses until it stops (it sends emails)
    try:
        uvicorn.run(app, **serve_options(settings, args.host, args.port))
    finally:
        lock.release()
        with contextlib.suppress(OSError):
            lock.path.unlink()


def _desktop(args: argparse.Namespace) -> None:
    from .desktop import run

    if code := run(args):
        sys.exit(code)


def _mcp(args: argparse.Namespace) -> None:
    if not args.http:
        from .mcp_server import build_server

        build_server().run()  # stdio: what Claude Desktop / Claude Code launch
        return

    # Standalone HTTP endpoint with the same bearer-token and Host checks as the dashboard's /mcp.
    from contextlib import AsyncExitStack, asynccontextmanager

    import uvicorn
    from fastapi import FastAPI

    from .config import get_settings, mcp_start_check
    from .mcp_server import mount_http

    settings = get_settings()
    check = mcp_start_check(settings, args.host)  # refuses a host other computers reach without an API token
    if check.refusals:
        sys.exit("openberry mcp: " + " ".join(check.refusals))
    for warning in check.warnings:
        logging.getLogger(__name__).warning("%s", warning)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            for hook in app.state.lifespan_hooks:
                await stack.enter_async_context(hook())
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.lifespan_hooks = []
    mount_http(app, settings)
    print(f"MCP endpoint: http://{args.host}:{args.port}/mcp", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _scan(args: argparse.Namespace) -> None:
    from . import repo
    from .collectors import get_collectors
    from .services import ScanInProgress, run_scan

    try:
        get_collectors(args.source)  # a typo fails before anything is scanned
    except ValueError as exc:
        sys.exit(f"openberry scan: error: {exc}")
    companies = repo.list_companies()
    if args.company and args.company not in {c.id for c in companies}:
        known = ", ".join(f"{c.id} ({c.name})" for c in companies) or "none yet"
        sys.exit(f"openberry scan: error: company {args.company} not found; registered companies: {known}")
    ids = [args.company] if args.company else [c.id for c in companies if c.status == "active"]
    if not ids:
        print("No companies registered yet. Open the dashboard (openberry serve) and register one.")
        return
    for company_id in ids:
        try:
            stats = asyncio.run(run_scan(company_id, trigger="cli", sources=args.source or None))
        except ScanInProgress as exc:
            stats = {"status": "already_running", "message": str(exc)}
        except repo.NotFound as exc:  # deleted while we were scanning the others
            stats = {"status": "not_found", "message": str(exc)}
        print(json.dumps({"company_id": company_id, **stats}, indent=2, default=str))


def _confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:  # no terminal to answer from
        print()
        return False


def _remove_sample(args: argparse.Namespace) -> None:
    from . import sample_cleanup

    ids = sorted(sample_cleanup.sample_company_ids())
    if args.company is not None:
        ids = [i for i in ids if i == args.company]
    if not ids:
        print(sample_cleanup.NOT_FOUND)
        return
    for company_id in ids:
        plan = sample_cleanup.cleanup_plan(company_id)
        if plan is None:
            continue
        print("\n".join(sample_cleanup.describe(plan)))
        if args.dry_run:
            print("Dry run: nothing was changed.")
            continue
        if not args.yes and not _confirm("Remove it? [y/N] "):
            print("Nothing was changed. Add --yes to remove it without being asked.")
            continue
        try:
            done = sample_cleanup.remove(company_id, plan.token)
        except sample_cleanup.PlanChanged as exc:
            sys.exit(f"openberry remove-sample: {exc}")
        if done.company_deleted:
            print(f"Deleted {plan.company.name}." + (f" Backup saved first: {done.backup}" if done.backup else ""))
        else:
            kept = "lead" if done.kept_leads == 1 else sample_cleanup.count_of(done.kept_leads, "lead")
            print(f"Removed {sample_cleanup.count_of(done.removed_leads, 'sample lead')}. {plan.company.name} and "
                  f"its other {kept} stay.")


def _rescore(_: argparse.Namespace) -> None:
    from . import repo

    for company in repo.list_companies():
        n = repo.rescore_company(company.id)
        print(f"{company.name}: rescored {n} leads")


def _init_db(_: argparse.Namespace) -> None:
    from .db import init_db

    print(f"Database ready at {init_db()}")


def _backup(args: argparse.Namespace) -> None:
    from . import backups, db

    if not db.db_path().is_file():
        sys.exit(f"openberry backup: there is no data to back up yet ({db.db_path()} doesn't exist).")
    db.prepare()  # a file a newer version wrote is refused, an older one is upgraded (with its own copy) first
    try:
        path = backups.manual_backup(args.path, overwrite=args.overwrite)
    except backups.BackupRefused as exc:
        sys.exit(f"openberry backup: {exc} Nothing was saved.")
    except (OSError, sqlite3.Error) as exc:
        sys.exit(f"openberry backup: the backup failed: {exc}")
    print(f"Backup saved: {path}")


def _restore(args: argparse.Namespace) -> None:
    from . import backups, db

    try:
        done = backups.restore(args.file)
    except backups.RestoreRefused as exc:
        sys.exit(f"openberry restore: {exc} Nothing was changed.")
    except (OSError, sqlite3.Error, db.DatabaseProblem) as exc:  # restore() refuses whatever fails before the swap
        sys.exit(f"openberry restore: the backup is now in place, and email sending and AI agent sending are off for "
                 f"every company. Something failed after that: {exc}")
    print(f"Restored {args.file}.")
    if done.previous:
        print(f"Your data from before the restore was saved first: {done.previous}")
    if done.kept_email_history:
        print("The never-email list and the record of sent emails were kept from it. So were the opt-outs, replies "
              "and sends it recorded for the leads and messages in the backup.")
    elif done.previous:
        print("It could not be read, so its never-email list, opt-outs, replies and record of sent messages were not "
              "kept.")
    if done.sending_turned_off:
        print("Email sending and AI agent sending are now off for: " + ", ".join(done.sending_turned_off) + ".")
    else:
        print("Email sending and AI agent sending are off for every company.")
    print("Leads and messages added after this backup was made are not in it: before you turn sending back on, "
          "check LinkedIn and your mailbox's Sent folder.")


def build_parser() -> argparse.ArgumentParser:
    from .desktop import add_arguments as add_desktop_arguments

    parser = argparse.ArgumentParser(prog="openberry", description="Open-source intent-signal lead generation.")
    parser.add_argument("--version", action="version", version=f"openberry {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web dashboard (and the HTTP MCP endpoint at /mcp)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_serve)

    p = sub.add_parser("desktop", help="open the dashboard in its own window, like an app")
    add_desktop_arguments(p)
    p.set_defaults(func=_desktop)

    p = sub.add_parser("mcp", help="run the MCP server for Claude (stdio by default)")
    p.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.set_defaults(func=_mcp)

    p = sub.add_parser("scan", help="run signal collectors now")
    p.add_argument("--company", type=int, help="company id (default: all active companies)")
    p.add_argument("--source", action="append", help="only this collector (repeatable), e.g. hackernews")
    p.set_defaults(func=_scan)

    sub.add_parser("rescore", help="recompute every lead score").set_defaults(func=_rescore)
    sub.add_parser("init-db", help="create the SQLite database").set_defaults(func=_init_db)

    p = sub.add_parser("backup", help="save a copy of your data (safe while OpenBerry runs)")
    p.add_argument("path", nargs="?", type=Path,
                   help="a folder to save a new copy in (a name without an extension is a folder, made if needed), "
                        "or a file name (default: the backups folder in your data folder)")
    p.add_argument("--overwrite", action="store_true", help="replace the file at PATH if there is one already")
    p.set_defaults(func=_backup)

    p = sub.add_parser("restore", help="put a backup back (close OpenBerry and quit Claude Desktop first)")
    p.add_argument("file", type=Path, help="the backup file, e.g. openberry-backup-2026-10-11.db")
    p.set_defaults(func=_restore)

    from .sample_cleanup import CLI_HELP

    p = sub.add_parser("remove-sample", help=CLI_HELP)
    p.add_argument("--company", type=int, help="only this company id")
    p.add_argument("--dry-run", action="store_true", help="show what would be removed and change nothing")
    p.add_argument("--yes", action="store_true", help="remove without asking")
    p.set_defaults(func=_remove_sample)
    return parser


def main(argv: list[str] | None = None) -> None:
    from .config import private_files

    private_files()  # the data folder, database and backups hold personal data: for this user only
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit:
        from .sample_cleanup import REMOVED_COMMAND, REMOVED_COMMAND_NOTE

        if argv[:1] == [REMOVED_COMMAND]:  # argparse said "invalid choice"; say why it's gone and what to do
            print(REMOVED_COMMAND_NOTE, file=sys.stderr)
        raise
    # Logs go to stderr: stdout belongs to the MCP protocol in `openberry mcp`.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs full request URLs at INFO; webhook URLs are secrets
    from .db import DatabaseProblem

    try:
        args.func(args)
    except DatabaseProblem as exc:  # a file a newer version wrote, an upgrade that couldn't save a copy first
        print(f"openberry: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
