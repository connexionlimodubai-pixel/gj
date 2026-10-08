"""Command line: `openberry serve | mcp | scan | demo | rescore | init-db`."""

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


def _mcp(args: argparse.Namespace) -> None:
    from .mcp_server import build_server

    server = build_server()
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run()  # stdio: what Claude Desktop / Claude Code launch


def _scan(args: argparse.Namespace) -> None:
    from . import repo
    from .services import run_scan

    ids = [args.company] if args.company else [c.id for c in repo.list_companies() if c.status == "active"]
    if not ids:
        print("No companies registered yet. Open the dashboard (openberry serve) and register one.")
        return
    for company_id in ids:
        stats = asyncio.run(run_scan(company_id, trigger="cli", sources=args.source or None))
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="openberry", description="Open-source intent-signal lead generation.")
    parser.add_argument("--version", action="version", version=f"openberry {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web dashboard (and the HTTP MCP endpoint at /mcp)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_serve)

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

    args = parser.parse_args(argv)
    # Logs go to stderr: stdout belongs to the MCP protocol in `openberry mcp`.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
