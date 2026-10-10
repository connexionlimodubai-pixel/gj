"""Google Maps businesses, the core parts: the profile field, the paid-API counter, the search state tables,
the Place ID identity key and scoring."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import openberry
from openberry import db, repo
from openberry.models import SIGNAL_SOURCES, SIGNAL_TYPES, CompanyIn, LeadIn, SignalConfig, SignalIn, split_lines
from openberry.scoring import SignalPoint, intent

SERVICE = "google_places_search"
PLACE = "ChIJN1t_tDeuEmsRUsoyG83frY4"
OTHER_PLACE = "ChIJ2eUgeAK6j4ARbn5u_wAGqWA"
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------
# Profile field
# --------------------------------------------------------------------------------------


def test_places_queries_split_on_line_breaks_only():
    config = SignalConfig(
        places_queries="law firms in DIFC, Dubai\r\n  event   planners in Dubai \n\nLaw firms in DIFC, Dubai")
    assert config.places_queries == ["law firms in DIFC, Dubai", "event planners in Dubai"]
    assert split_lines(["hotels in Dubai\nDMCs in Dubai", "hotels in dubai", 7]) == [
        "hotels in Dubai", "DMCs in Dubai", "7"]
    assert split_lines(None) == []
    # A profile saved through the API or Claude keeps a list item with a comma whole.
    assert CompanyIn(name="X", signals={"places_queries": ["banks in Abu Dhabi, UAE"]}).signals.places_queries == [
        "banks in Abu Dhabi, UAE"]


def test_places_queries_limits():
    with pytest.raises(ValidationError, match="At most 30 Google Maps searches"):
        SignalConfig(places_queries=[f"hotels in area {n}" for n in range(31)])
    with pytest.raises(ValidationError, match="under 200 characters"):
        SignalConfig(places_queries=["x" * 201])
    assert len(SignalConfig(places_queries=[f"q{n}" for n in range(30)]).places_queries) == 30


def test_business_search_vocabulary():
    assert SIGNAL_TYPES["business_search"] == ("Matches a business search you set up", 10)
    assert list(SIGNAL_TYPES)[-1] == "custom"
    assert "google_places" in SIGNAL_SOURCES
    assert SignalIn(type="business_search").type == "business_search"


# --------------------------------------------------------------------------------------
# Paid-API counter
# --------------------------------------------------------------------------------------


def test_reserve_api_call_grants_until_the_limit():
    results = [repo.reserve_api_call(SERVICE, 3, now=NOW) for _ in range(5)]
    assert [r.granted for r in results] == [True, True, True, False, False]
    assert [r.used for r in results] == [1, 2, 3, 3, 3]
    assert results[-1].remaining == 0 and results[0].remaining == 2
    assert repo.api_usage(SERVICE, 3, now=NOW).used == 3
    # A new calendar month (UTC) starts at 0; another service has its own count.
    november = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    assert repo.reserve_api_call(SERVICE, 3, now=november).used == 1
    assert repo.api_usage("other_service", 3, now=NOW).used == 0
    # October 31 at 23:00 in Los Angeles is already November in UTC.
    late = datetime(2026, 10, 31, 23, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert repo.usage_month(late) == "2026-11"


def test_limit_zero_never_grants_and_reading_creates_no_row():
    assert not repo.reserve_api_call(SERVICE, 0, now=NOW).granted
    assert repo.api_usage(SERVICE, 900, now=NOW).used == 0
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


def test_reserve_api_call_is_atomic_across_threads():
    granted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            ok = repo.reserve_api_call(SERVICE, 50, now=NOW).granted
            with lock:
                granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 50 and len(granted) == 200
    assert repo.api_usage(SERVICE, 50, now=NOW).used == 50


CHILD = """
import os, sys
os.environ["OPENBERRY_DB"] = sys.argv[1]
os.environ["OPENBERRY_ENV_FILE"] = sys.argv[2]
from openberry import repo
print(sum(repo.reserve_api_call("google_places_search", 50).granted for _ in range(40)))
"""


def test_reserve_api_call_is_atomic_across_processes(settings, tmp_path: Path):
    repo.api_usage(SERVICE, 50)  # create the database first
    src = str(Path(openberry.__file__).resolve().parents[1])
    env = {**__import__("os").environ, "PYTHONPATH": src}
    args = [str(settings.db_path), str(tmp_path / "missing.env")]
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env) for _ in range(2)]
    outputs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), outputs
    assert sum(int(out.strip()) for out, _ in outputs) == 50
    assert repo.api_usage(SERVICE, 50).used == 50


# --------------------------------------------------------------------------------------
# Search state and Place IDs
# --------------------------------------------------------------------------------------


def test_place_search_state(company):
    repo.finish_place_search(company.id, "hotels in dubai", 40, 12, when=NOW - timedelta(days=3))
    repo.finish_place_search(company.id, "dmcs in dubai", 5, 1, when=NOW - timedelta(days=9))
    repo.finish_place_search(company.id, "hotels in dubai", 41, 2, when=NOW)
    assert repo.place_search_times(company.id) == {"hotels in dubai": NOW, "dmcs in dubai": NOW - timedelta(days=9)}
    assert repo.prune_place_searches(company.id, {"hotels in dubai"}) == 1
    assert list(repo.place_search_times(company.id)) == ["hotels in dubai"]
    assert repo.prune_place_searches(company.id, set()) == 1
    assert repo.place_search_times(company.id) == {}


def test_handled_place_ids(company):
    repo.record_place_ids(company.id, {PLACE: True, OTHER_PLACE: False}, when=NOW - timedelta(days=40))
    recheck_before = NOW - timedelta(days=30)
    # Added ones are skipped forever; skipped ones only until they are 30 days old.
    assert repo.handled_place_ids(company.id, [PLACE, OTHER_PLACE, "ChIJunknownPlaceId00"], recheck_before) == {PLACE}
    repo.record_place_ids(company.id, {OTHER_PLACE: False, PLACE: False}, when=NOW)
    assert repo.handled_place_ids(company.id, [PLACE, OTHER_PLACE], recheck_before) == {PLACE, OTHER_PLACE}
    with db.connect() as c:  # added never goes back to 0
        assert c.execute("SELECT added FROM place_ids WHERE place_id = ?", (PLACE,)).fetchone()[0] == 1
    many = [f"ChIJmanyPlaceIds{n:05d}" for n in range(1200)]
    repo.record_place_ids(company.id, {pid: True for pid in many}, when=NOW)
    assert repo.handled_place_ids(company.id, many, recheck_before) == set(many)
    assert repo.handled_place_ids(company.id, [], recheck_before) == set()
    repo.delete_company(company.id)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM place_ids").fetchone()[0] == 0


# --------------------------------------------------------------------------------------
# Identity: the Place ID merges the same business
# --------------------------------------------------------------------------------------


def test_google_place_id_reads_maps_links():
    assert repo.maps_place_url(PLACE) == f"https://www.google.com/maps/place/?q=place_id:{PLACE}"
    assert repo.google_place_id(repo.maps_place_url(PLACE)) == PLACE
    assert repo.google_place_id(f"https://www.google.com/maps/search/?api=1&query=Acme&query_place_id={PLACE}") == PLACE
    assert repo.google_place_id(f"https://maps.google.com/maps/place/?q=place_id%3A{PLACE}#x") == PLACE
    for url in ("", f"https://evil.example/maps/place/?q=place_id:{PLACE}", "https://www.google.com/maps/place/Acme",
                f"https://www.google.com/maps/place/?q=place_id:{PLACE}<script>", "not a url"):
        assert repo.google_place_id(url) == "", url


def test_accounts_merge_by_place_id_and_lose_the_key_as_a_person(company):
    maps = repo.maps_place_url(PLACE)
    first, created = repo.upsert_lead(company.id, LeadIn(lead_company="Acme Events", profile_url=maps))
    assert created and "acct:gp:" + PLACE in repo.lead_identity_keys(first, "account")
    again, created = repo.upsert_lead(company.id, LeadIn(lead_company="ACME Event Management L.L.C",
                                                         company_domain="acme-events.ae", profile_url=maps))
    assert not created and again.id == first.id and again.company_domain == "acme-events.ae"
    # A person's profile URL never becomes an account key.
    assert not any(k.startswith("acct:") for k in repo.lead_identity_keys(
        LeadIn(full_name="Jane Doe", lead_company="Acme Events", profile_url=maps), "person"))
    repo.update_lead(first.id, {"full_name": "Jane Doe"})
    with db.connect() as c:
        keys = [r[0] for r in c.execute("SELECT key FROM lead_keys WHERE lead_id = ?", (first.id,))]
    assert not any(k.startswith("acct:") for k in keys)
    third, created = repo.upsert_lead(company.id, LeadIn(lead_company="Other Name Ltd", profile_url=maps))
    assert created and third.id != first.id


# --------------------------------------------------------------------------------------
# Schema: the new tables arrive without a version change
# --------------------------------------------------------------------------------------


def test_an_existing_database_gets_the_new_tables_and_keeps_its_version(settings):
    db.init_db(settings.db_path)
    conn = sqlite3.connect(settings.db_path)
    for table in ("api_usage", "place_searches", "place_ids"):
        conn.execute(f"DROP TABLE {table}")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.commit()
    conn.close()
    assert version == db.SCHEMA_VERSION

    db.reset_init_cache()
    assert repo.api_usage(SERVICE, 900).used == 0  # first connect upgrades the file
    conn = sqlite3.connect(settings.db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"api_usage", "place_searches", "place_ids"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    conn.close()


# --------------------------------------------------------------------------------------
# Scoring: a prospect list, not intent
# --------------------------------------------------------------------------------------


def test_business_search_adds_little_and_never_stacks():
    now = datetime.now(timezone.utc)
    alone, reasons = intent([SignalPoint("business_search", 10, now)], now=now)
    assert alone == 3
    assert any("Matches a business search you set up" in r for r in reasons)
    hiring, _ = intent([SignalPoint("hiring", 50, now)], now=now)
    both, reasons = intent([SignalPoint("hiring", 50, now), SignalPoint("business_search", 10, now)], now=now)
    assert not any("stacking" in r for r in reasons)
    assert both - hiring <= 2
    stacked, reasons = intent([SignalPoint("hiring", 50, now), SignalPoint("funding", 50, now)], now=now)
    assert any("stacking" in r for r in reasons)
