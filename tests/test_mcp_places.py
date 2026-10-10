"""MCP side of Google Maps businesses: Claude sees the searches and this month's usage, never the key."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.client import Client

from openberry import mcp_server, repo, services
from openberry.collectors import google_places
from openberry.mcp_server import build_server
from places_fakes import QUERY

SECRET_KEY = "AIzaSECRETMARKER_0123456789abcdefghijk"


@asynccontextmanager
async def mcp_client() -> AsyncIterator[Client]:
    async with Client(build_server()) as client:
        yield client


async def ok(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_profile_shows_usage_but_never_the_key(company, settings, monkeypatch):
    monkeypatch.setattr(settings, "google_places_key", SECRET_KEY)
    repo.update_company(company.id, {"signals": {"places_queries": [QUERY]}})
    repo.reserve_api_call(google_places.SERVICE, 900)
    async with mcp_client() as c:
        profile = await ok(c, "get_company_profile", company_id=company.id)
    row = next(r for r in profile["collectors"] if r["name"] == "google_places")
    assert row["enabled"] and row["label"] == "Google Maps businesses"
    assert row["usage"]["searches_used_this_month"] == 1 and row["usage"]["monthly_limit"] == 900
    assert row["usage"]["api_key_set"] is True and row["usage"]["searches_per_scan"] == 10
    assert profile["profile"]["signals"]["places_queries"] == [QUERY]
    assert "SECRETMARKER" not in json.dumps(profile)
    others = [r for r in profile["collectors"] if r["name"] != "google_places"]
    assert all("usage" not in r for r in others)


async def test_update_company_sets_the_searches(company):
    async with mcp_client() as c:
        updated = await ok(c, "update_company", company_id=company.id,
                           changes={"signals": {"places_queries": ["law firms in DIFC, Dubai", "DMCs in Dubai"]}})
    assert updated["profile"]["signals"]["places_queries"] == ["law firms in DIFC, Dubai", "DMCs in Dubai"]
    assert repo.get_company(company.id).signals.places_queries == ["law firms in DIFC, Dubai", "DMCs in Dubai"]


async def test_run_signal_scan_reports_per_collector_counts(company, monkeypatch):
    counts = {"searches": 3, "businesses": 11, "added": 6, "no_website": 2}

    async def fake_run_scan(company_id, *, trigger="manual", sources=None, client=None):
        return {"status": "ok", "run_id": 1, "collectors": {"google_places": {"found": 6, "warnings": [],
                                                                              "counts": counts}},
                "skipped": [], "signals_new": 6, "signals_duplicate": 0, "leads_new": 6, "leads_updated": 0,
                "newly_hot": [], "notified": [], "drafted": 0, "errors": []}

    monkeypatch.setattr(services, "run_scan", fake_run_scan)
    mcp_server._scan_tasks.clear()
    async with mcp_client() as c:
        result = await ok(c, "run_signal_scan", company_id=company.id, sources=["google_places"])
    mcp_server._scan_tasks.clear()
    assert result["collectors"]["google_places"] == {"found": 6, "counts": counts}


async def test_no_tool_reads_or_sets_keys():
    async with mcp_client() as c:
        tools = (await c.list_tools()).tools
    for tool in tools:
        properties = json.dumps(tool.input_schema.get("properties", {})).lower()
        assert "api_key" not in properties and "google_places_key" not in properties, tool.name
    names = {t.name for t in tools}
    assert not any("key" in name for name in names)
    scan = next(t for t in tools if t.name == "run_signal_scan")
    assert "Google Maps businesses" in " ".join(scan.description.split())
    assert "google_places" in json.dumps(scan.input_schema)
    assert "Google Maps businesses" in " ".join(mcp_server.INSTRUCTIONS.split())
