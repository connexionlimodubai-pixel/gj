from datetime import datetime, timedelta, timezone

import pytest

from openberry import repo
from openberry.collectors import RawSignal
from openberry.leads_csv import export_leads_csv, import_leads_csv
from openberry.models import ICP, CompanyIn, LeadIn, SignalIn
from openberry.scoring import LeadFacts, SignalPoint, icp_fit, infer_seniority, intent, size_bucket
from openberry.services import ingest


def test_split_list_and_job_board_parsing():
    c = CompanyIn(name="X", competitors="A, B\nA", signals={"job_boards": "greenhouse:stripe:Stripe\nlever:foo"})
    assert c.competitors == ["A", "B"]
    assert [(b.provider, b.token, b.company) for b in c.signals.job_boards] == [
        ("greenhouse", "stripe", "Stripe"), ("lever", "foo", "foo")]


def test_seniority_and_size():
    assert infer_seniority("Executive Assistant to the CEO") is None
    assert infer_seniority("VP of Sales") == "vp"
    assert infer_seniority("Co-Founder & CEO") == "founder"
    assert size_bucket("120 employees") == "51-200"
    assert size_bucket("10k+") == "5000+"


def test_icp_fit_full_match_and_exclusion():
    icp = ICP(job_titles=["Travel Manager"], locations=["UAE"], exclude_keywords=["student"])
    score, reasons, dq = icp_fit(LeadFacts(title="Senior Travel Manager", location="Dubai, United Arab Emirates"), icp)
    assert score == 100 and not dq
    score, reasons, dq = icp_fit(LeadFacts(title="Student"), icp)
    assert dq and score == 0


def test_intent_decay_and_stacking():
    now = datetime.now(timezone.utc)
    fresh, _ = intent([SignalPoint("competitor_engagement", 50, now)], now=now)
    old, _ = intent([SignalPoint("competitor_engagement", 50, now - timedelta(days=42))], now=now)
    assert fresh > old > 0
    stacked, reasons = intent([SignalPoint("competitor_engagement", 50, now), SignalPoint("hiring", 50, now)], now=now)
    assert any("stacking" in r for r in reasons)


def test_upsert_merges_by_identity(company):
    a, created = repo.upsert_lead(company.id, LeadIn(full_name="Jane Doe", linkedin_url="https://www.linkedin.com/in/janedoe/"))
    assert created
    b, created = repo.upsert_lead(company.id, LeadIn(full_name="Jane D.", linkedin_url="linkedin.com/in/JaneDoe?trk=1",
                                                     email="jane@x.com", title="Travel Manager"))
    assert not created and b.id == a.id and b.full_name == "Jane Doe" and b.title == "Travel Manager"
    c, created = repo.upsert_lead(company.id, LeadIn(full_name="Someone", email="JANE@x.com"))
    assert c.id == a.id


def test_account_signal_lifts_people_at_that_company(company):
    person, _ = repo.upsert_lead(company.id, LeadIn(full_name="Omar", title="Travel Manager", lead_company="Northwind LLC"))
    stats = ingest(company.id, [RawSignal(signal=SignalIn(type="hiring", title="Hiring EA", source="lever", external_id="1"),
                                          account="Northwind")])
    assert stats.signals_new == 1
    after = repo.get_lead(person.id)
    assert after.intent_score > person.intent_score
    again = ingest(company.id, [RawSignal(signal=SignalIn(type="hiring", title="Hiring EA", source="lever", external_id="1"),
                                          account="Northwind")])
    assert again.signals_duplicate == 1


def test_messages_followups_and_reply(company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Ann", title="Travel Manager"))
    msg = repo.create_message(lead.id, "Hi Ann")
    repo.update_message(msg.id, status="sent")
    assert repo.get_lead(lead.id).status == "contacted"
    later = repo.utcnow() + timedelta(days=4)
    assert [d["lead"].id for d in repo.followups_due(company.id, now=later)] == [lead.id]
    repo.log_reply(lead.id, "Interested!")
    assert repo.get_lead(lead.id).status == "replied"
    assert repo.followups_due(company.id, now=later) == []


def test_update_company_partial_merge(company):
    updated = repo.update_company(company.id, {"icp": {"locations": ["KSA"]}})
    assert updated.icp.locations == ["KSA"] and updated.icp.job_titles == ["Executive Assistant", "Travel Manager"]


def test_csv_roundtrip_and_formula_escape(company):
    stats = import_leads_csv(company.id, "First Name,Last Name,Title,Company\nAl,=cmd,Travel Manager,Acme\n,,,\n")
    assert stats["created"] == 1 and stats["skipped"] == 1
    out = export_leads_csv(repo.list_leads(company.id)[0])
    assert "'=cmd" not in out.splitlines()[0] and "Al '=cmd" not in out  # name is 'Al =cmd' -> not at cell start
    stats = import_leads_csv(company.id, "Name,Company\n=HYPERLINK(1),Evil\n")
    assert "'=HYPERLINK(1)" in export_leads_csv(repo.list_leads(company.id, search="HYPERLINK")[0])


def test_lead_requires_name_or_company(company):
    with pytest.raises(ValueError):
        repo.upsert_lead(company.id, LeadIn(title="CEO"))
