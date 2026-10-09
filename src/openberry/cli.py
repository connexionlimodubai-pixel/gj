"""Command line: `openberry serve | desktop | mcp | scan | demo | rescore | init-db`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import __version__


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info", proxy_headers=True)


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

    from .config import get_settings
    from .mcp_server import mount_http

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            for hook in app.state.lifespan_hooks:
                await stack.enter_async_context(hook())
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.lifespan_hooks = []
    mount_http(app, get_settings())
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


def _demo(_: argparse.Namespace) -> None:
    from .seed import seed_demo

    company_id = seed_demo()
    print(f"Demo company created with id {company_id}. Run `openberry serve` and open http://127.0.0.1:8000")


def _rescore(_: argparse.Namespace) -> None:
    from . import repo

    for company in repo.list_companies():
        n = repo.rescore_company(company.id)
        print(f"{company.name}: rescored {n} leads")


def _init_db(_: argparse.Namespace) -> None:
    from .db import init_db

    print(f"Database ready at {init_db()}")


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

    sub.add_parser("demo", help="create a demo company with sample leads").set_defaults(func=_demo)
    sub.add_parser("rescore", help="recompute every lead score").set_defaults(func=_rescore)
    sub.add_parser("init-db", help="create the SQLite database").set_defaults(func=_init_db)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # Logs go to stderr: stdout belongs to the MCP protocol in `openberry mcp`.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs full request URLs at INFO; webhook URLs are secrets
    args.func(args)


if __name__ == "__main__":
    main()
