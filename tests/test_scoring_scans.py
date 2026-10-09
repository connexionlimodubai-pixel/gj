"""Scan bookkeeping (one scan at a time, honest statuses, retries), hot-lead alerts and scoring fixes."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from openberry import cli, db, repo, scheduler, services
from openberry.collectors import RawSignal
from openberry.models import ICP, CompanyIn, LeadIn, SignalIn
from openberry.scoring import LeadFacts, SignalPoint, icp_fit, intent, normalize_seniority, size_buckets

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


class FakeCollector:
    """Stands in for a real collector: returns `raw`, warns `warnings`, or raises `error`."""

    label = "Fake"
    signal_types = ("keyword_mention",)
    requires = ""

    def __init__(self, name: str, raw: list[RawSignal] | None = None, warnings: tuple[str, ...] = (),
                 error: Exception | None = None, gate: asyncio.Event | None = None):
        self.name, self.raw, self.warnings, self.error, self.gate = name, raw or [], warnings, error, gate

    def is_configured(self, company) -> bool:
        return True

    def enabled_for(self, company) -> bool:
        return True

    async def collect(self, company, ctx) -> list[RawSignal]:
        if self.gate is not None:
            await self.gate.wait()
        for w in self.warnings:
            ctx.warn(w)
        if self.error is not None:
            raise self.error
        return self.raw


def use_collectors(monkeypatch: pytest.MonkeyPatch, *collectors: FakeCollector) -> None:
    monkeypatch.setattr(services, "get_collectors", lambda names=None: list(collectors))


def hot_lead_in(name: str = "Ann Hot") -> LeadIn:
    """A perfect ICP fit for the conftest company with three fresh strong signals: scores ~99."""
    now = datetime.now(timezone.utc)
    return LeadIn(full_name=name, title="Travel Manager", lead_company="Gulf Bank", industry="Finance",
                  company_size="120", location="Dubai", bio="Runs corporate travel",
                  signals=[SignalIn(type=t, title=f"{name} {t}", strength=100, occurred_at=now)
                           for t in ("competitor_engagement", "profile_visit", "job_change")])


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    """Records the lead ids of every hot-lead alert instead of posting webhooks."""
    sent: list[list[int]] = []

    async def fake_notify(company, leads, client=None):
        sent.append([lead.id for lead in leads])
        return ["slack"]

    monkeypatch.setattr(services, "notify_hot_leads", fake_notify)
    return sent


def ago(**delta: float) -> datetime:
    return repo.utcnow() - timedelta(**delta)


# --------------------------------------------------------------------------------------
# INT-02: one scan per company at a time, across processes
# --------------------------------------------------------------------------------------


def test_start_scan_run_refuses_while_another_scan_runs(company):
    first = repo.start_scan_run(company.id, "dashboard")
    with pytest.raises(repo.ScanInProgress, match="started from dashboard") as err:
        repo.start_scan_run(company.id, "schedule")
    assert err.value.run is not None and err.value.run.id == first
    assert [r.id for r in repo.list_scan_runs(company.id)] == [first]

    # A row left 'running' by a process that died long ago doesn't block, nor does a finished run.
    with db.connect() as c:
        c.execute("UPDATE scan_runs SET started_at = ? WHERE id = ?", (repo.iso(ago(minutes=20)), first))
    second = repo.start_scan_run(company.id, "cli")
    repo.finish_scan_run(second, "ok", {})
    assert repo.start_scan_run(company.id, "cli") > second


def test_only_one_of_many_simultaneous_scans_starts(company):
    """The check and the insert are atomic, so processes racing to scan can't both win."""
    barrier = threading.Barrier(8)
    started: list[int] = []
    refused: list[Exception] = []

    def attempt() -> None:
        barrier.wait()
        try:
            started.append(repo.start_scan_run(company.id, "race"))
        except repo.ScanInProgress as exc:
            refused.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(started) == 1 and len(refused) == 7


async def test_scheduler_and_cli_skip_a_company_whose_scan_is_running(company, monkeypatch, alerts, capsys):
    repo.update_company(company.id, {"notify": {"slack_webhook_url": "https://hooks.slack.com/services/x"}})
    gate = asyncio.Event()
    lead_signal = RawSignal(signal=SignalIn(type="keyword_mention", title="t", source="hackernews", external_id="1",
                                            strength=100), lead=hot_lead_in())
    use_collectors(monkeypatch, FakeCollector("slow", raw=[lead_signal], gate=gate))
    dashboard = asyncio.create_task(services.run_scan(company.id, trigger="dashboard"))
    for _ in range(200):
        if repo.list_scan_runs(company.id):
            break
        await asyncio.sleep(0.01)

    assert repo.companies_due_for_scan() == []
    assert await services.scan_due_companies() == []
    with pytest.raises(services.ScanInProgress):
        await services.run_scan(company.id, trigger="schedule")
    gate.set()
    stats = await dashboard
    assert stats["status"] == "ok"
    [run] = repo.list_scan_runs(company.id)
    assert run.trigger == "dashboard" and run.status == "ok"  # the refused attempts touched nothing
    assert alerts == [stats["newly_hot"]] and len(stats["newly_hot"]) == 1  # one alert, not two

    # The CLI says so politely instead of failing.
    repo.start_scan_run(company.id, "dashboard")
    await asyncio.to_thread(cli.main, ["scan", "--company", str(company.id)])
    out = capsys.readouterr().out
    assert '"status": "already_running"' in out and "Traceback" not in out


# --------------------------------------------------------------------------------------
# INT-16: friendly CLI errors
# --------------------------------------------------------------------------------------


def test_cli_scan_reports_unknown_company_and_source_without_traceback(company, capsys):
    with pytest.raises(SystemExit) as err:
        cli.main(["scan", "--company", "999"])
    assert err.value.code == f"openberry scan: error: company 999 not found; registered companies: {company.id} (Acme Chauffeurs)"
    with pytest.raises(SystemExit) as err:
        cli.main(["scan", "--company", str(company.id), "--source", "bogus"])
    assert str(err.value.code).startswith("openberry scan: error: unknown collector(s): bogus; available: hackernews")
    assert repo.list_scan_runs(company.id) == []  # nothing was started


# --------------------------------------------------------------------------------------
# INT-09 / J3: honest scan statuses, and quicker retries after a failure
# --------------------------------------------------------------------------------------


async def test_scan_where_every_source_failed_is_recorded_as_failed(company, monkeypatch):
    use_collectors(monkeypatch,
                   FakeCollector("hackernews", warnings=("Hacker News: search failed (ProxyError)",)),
                   FakeCollector("jobs", warnings=("greenhouse board 'x': request failed (ProxyError)",)),
                   FakeCollector("rss", error=RuntimeError("boom")))
    stats = await services.run_scan(company.id)
    assert stats["status"] == "failed" and "every one failed" in stats["error"]
    run = repo.get_scan_run(stats["run_id"])
    assert run.status == "failed" and run.stats["error"] == stats["error"]


async def test_scan_is_ok_when_one_source_worked_or_came_back_clean(company, monkeypatch):
    use_collectors(monkeypatch, FakeCollector("hackernews", warnings=("Hacker News: search failed",)),
                   FakeCollector("github"))  # no results and no problems: nothing happened, that's fine
    stats = await services.run_scan(company.id)
    assert stats["status"] == "ok" and "error" not in stats


async def test_scan_with_no_source_configured_says_so():
    quiet = repo.create_company(CompanyIn(name="Quiet Co"))
    stats = await services.run_scan(quiet.id)
    assert stats["status"] == "nothing_configured" and "No signal source is configured" in stats["error"]
    assert repo.list_scan_runs(quiet.id)[0].status == "nothing_configured"
    assert repo.get_company(quiet.id).last_scan_at is not None


def test_failed_scan_is_retried_after_an_hour_not_a_full_interval(company):
    def scanned(status: str, hours_ago: float) -> None:
        run_id = repo.start_scan_run(company.id)
        repo.finish_scan_run(run_id, status, {})
        repo.set_last_scan(company.id, ago(hours=hours_ago))

    scanned("failed", 0.5)
    assert repo.companies_due_for_scan() == []
    scanned("failed", 1.5)
    assert [c.id for c in repo.companies_due_for_scan()] == [company.id]
    scanned("ok", 1.5)
    assert repo.companies_due_for_scan() == []  # a good scan waits its scan_interval_hours (24)
    scanned("ok", 25)
    assert [c.id for c in repo.companies_due_for_scan()] == [company.id]


async def test_crashed_scan_waits_an_hour_before_the_scheduler_retries(company, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(services, "_scan", boom)
    with pytest.raises(RuntimeError):
        await services.run_scan(company.id)
    assert repo.list_scan_runs(company.id)[0].status == "failed"
    assert repo.get_company(company.id).last_scan_at is not None
    assert repo.companies_due_for_scan() == []
    assert repo.companies_due_for_scan(now=repo.utcnow() + timedelta(minutes=61)) != []


# --------------------------------------------------------------------------------------
# J6 / DOC-1: alerts and auto-drafts for leads that turn hot outside a scan
# --------------------------------------------------------------------------------------


async def test_lead_that_turns_hot_outside_a_scan_is_alerted_and_drafted_once(company, alerts):
    repo.update_company(company.id, {"outreach": {"mode": "auto_draft"}})
    lead, _ = repo.upsert_lead(company.id, hot_lead_in())  # e.g. Claude's add_leads, the API or a CSV import
    assert lead.tier == "hot"

    result = await services.alert_new_hot_leads(company.id)
    assert result == {"newly_hot": [lead.id], "notified": ["slack"], "drafted": 1}
    assert alerts == [[lead.id]] and len(repo.list_messages(company.id, lead_id=lead.id)) == 1
    assert await services.alert_new_hot_leads(company.id) == {"newly_hot": [], "notified": [], "drafted": 0}
    assert alerts == [[lead.id]]


async def test_scan_alerts_leads_that_were_already_hot_before_it(company, monkeypatch, alerts):
    lead, _ = repo.upsert_lead(company.id, hot_lead_in())
    use_collectors(monkeypatch, FakeCollector("github"))
    stats = await services.run_scan(company.id)
    assert stats["newly_hot"] == [lead.id] and alerts == [[lead.id]]
    assert (await services.run_scan(company.id))["newly_hot"] == []


async def test_scheduler_tick_alerts_active_companies_only(company, alerts):
    paused = repo.create_company(CompanyIn(name="Paused Co", status="paused", icp=company.icp))
    lead, _ = repo.upsert_lead(company.id, hot_lead_in())
    repo.upsert_lead(paused.id, hot_lead_in("Pat Paused"))
    repo.set_last_scan(company.id)  # not due: the tick only alerts
    repo.set_last_scan(paused.id)

    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.scheduler_loop(stop))
    for _ in range(200):
        if alerts:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert alerts == [[lead.id]]


async def test_lead_that_went_cold_is_alerted_again_when_hot_again(company, alerts):
    lead, _ = repo.upsert_lead(company.id, hot_lead_in())
    await services.alert_new_hot_leads(company.id)
    repo.update_lead(lead.id, {"status": "disqualified"})
    repo.update_lead(lead.id, {"status": "qualified"})
    await services.alert_new_hot_leads(company.id)
    assert alerts == [[lead.id], [lead.id]]


def test_upgrading_an_old_database_adds_alerted_at_without_re_alerting(settings):
    old_schema = re.sub(r"\n\s*alerted_at TEXT,[^\n]*", "", db.SCHEMA)
    assert "alerted_at" not in old_schema
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(old_schema)
    now = repo.iso()
    conn.execute("INSERT INTO companies (name, created_at, updated_at) VALUES ('Old Co', ?, ?)", (now, now))
    conn.executemany("INSERT INTO leads (company_id, full_name, tier, score, created_at, updated_at) "
                     "VALUES (1, ?, ?, ?, ?, ?)", [("Was Hot", "hot", 90, now, now), ("Was Warm", "warm", 50, now, now)])
    conn.commit()
    conn.close()

    with db.connect() as c:
        rows = {r["full_name"]: r["alerted_at"] for r in c.execute("SELECT full_name, alerted_at FROM leads")}
    assert rows == {"Was Hot": now, "Was Warm": None}
    db.init_db(settings.db_path)  # running the migration again is harmless
    assert repo.claim_new_hot_leads(1) == []


# --------------------------------------------------------------------------------------
# INT-10: disqualified leads drop out of score-sorted lists
# --------------------------------------------------------------------------------------


def test_disqualified_lead_score_is_capped_and_restored(company):
    lead, _ = repo.upsert_lead(company.id, hot_lead_in())
    assert lead.score >= 90
    dq = repo.update_lead(lead.id, {"status": "disqualified"})
    assert dq.score <= 15 and dq.tier == "cold" and dq.score_reasons[0] == "! Disqualified (pipeline status)"
    assert repo.list_leads(company.id, min_score=60)[1] == 0
    back = repo.update_lead(lead.id, {"status": "new"})
    assert back.score == lead.score and back.tier == "hot"


# --------------------------------------------------------------------------------------
# INT-12: ICP seniority / size labels and aliases
# --------------------------------------------------------------------------------------


def test_icp_seniority_and_size_labels_count_as_their_keys():
    cfo = LeadFacts(title="Chief Financial Officer", company_size="120 employees")
    by_key = icp_fit(cfo, ICP(seniorities=["c_level"], company_sizes=["51-200"]))
    by_label = icp_fit(cfo, ICP(seniorities=["C-level", "Head of"], company_sizes=["50-200"]))
    assert by_label == by_key and by_key[0] == 100


def test_unknown_icp_seniority_and_size_values_are_ignored_not_penalised():
    lead = LeadFacts(title="Travel Manager", company_size="120")
    plain = icp_fit(lead, ICP(job_titles=["Travel Manager"]))
    odd = icp_fit(lead, ICP(job_titles=["Travel Manager"], seniorities=["big shots"], company_sizes=["huge"]))
    assert odd == plain and odd[0] == 100


def test_seniority_and_size_vocabulary():
    assert [normalize_seniority(s) for s in ("C-level", "c_level", "C-Suite", "Head of", "VP Sales", "Owner",
                                             "Founder / Owner", "Directors", "Entry level", "big shots")] == [
        "c_level", "c_level", "c_level", "head", "vp", "founder", "founder", "director", "entry", None]
    assert size_buckets("51-200") == ["51-200"]
    assert size_buckets("50-200") == ["51-200"]
    assert size_buckets("10-1,000 employees") == ["11-50", "51-200", "201-1000"]
    assert size_buckets("1000+") == ["1001-5000", "5000+"]
    assert size_buckets("120") == ["51-200"]
    assert size_buckets("big") == []


# --------------------------------------------------------------------------------------
# INT-13: future-dated signals
# --------------------------------------------------------------------------------------


def test_future_signal_dates_are_stored_as_now(company):
    future = datetime.now(timezone.utc) + timedelta(days=60)
    assert SignalIn(occurred_at=future).occurred_at <= datetime.now(timezone.utc)
    assert SignalIn(occurred_at=future.replace(tzinfo=None)).occurred_at <= datetime.now(timezone.utc)
    past = datetime(2026, 1, 2, 3, 4)
    assert SignalIn(occurred_at=past).occurred_at == past

    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Eve", signals=[
        SignalIn(type="event", title="Attending GITEX", occurred_at=future.isoformat())]))
    [signal], _ = repo.list_signals(company.id, lead_id=lead.id)
    assert signal.occurred_at <= datetime.now(timezone.utc)
    assert repo.get_lead(lead.id).last_signal_at <= datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# INT-15: weight 0 means ignored
# --------------------------------------------------------------------------------------


def test_signal_type_weighted_zero_adds_no_stacking_bonus_or_reason():
    now = datetime.now(timezone.utc)
    weights = {"company_news": 0}
    alone, _ = intent([SignalPoint("keyword_mention", 50, now)], weights, now)
    both, reasons = intent([SignalPoint("keyword_mention", 50, now), SignalPoint("company_news", 50, now)],
                           weights, now)
    assert both == alone
    assert not any("stacking" in r or "news" in r for r in reasons)


# --------------------------------------------------------------------------------------
# J17: the industry reason names the lead's own industry
# --------------------------------------------------------------------------------------


def test_industry_reason_prefers_the_leads_industry_field():
    icp = ICP(industries=["Finance", "Hospitality", "Events", "Consulting"])
    _, reasons, _ = icp_fit(LeadFacts(industry="Consulting", bio="I love organising events"), icp)
    assert "+ Industry matches 'Consulting'" in reasons


# --------------------------------------------------------------------------------------
# J13: replies received show up in the stats
# --------------------------------------------------------------------------------------


def test_stats_count_replies_received(company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Ann", title="Travel Manager"))
    repo.update_message(repo.create_message(lead.id, "Hi Ann").id, status="sent")
    repo.log_reply(lead.id, "Interested!")
    messages = repo.company_stats(company.id)["messages"]
    assert messages["sent"] == 1 and messages["received"] == 1
