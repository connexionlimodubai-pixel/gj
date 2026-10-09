"""Lead identity (merging, company keys, Unicode names) and CSV import/export round trips."""

from __future__ import annotations

import pytest

from openberry import repo
from openberry.collectors import RawSignal
from openberry.leads_csv import export_leads_csv, import_leads_csv
from openberry.models import CompanyIn, LeadIn, SignalIn
from openberry.services import ingest


def _account_signal(account: str, ext: str, domain: str = "", strength: int = 100) -> RawSignal:
    return RawSignal(signal=SignalIn(type="funding", title=f"{account or domain} raised", source="news",
                                     external_id=ext, strength=strength), account=account, account_domain=domain)


def _keys(lead_id: int) -> list[str]:
    from openberry.db import connect

    with connect() as c:
        return sorted(r["key"] for r in c.execute("SELECT key FROM lead_keys WHERE lead_id = ?", (lead_id,)))


# --------------------------------------------------------------------------------------
# INT-07: Unicode names
# --------------------------------------------------------------------------------------


def test_non_latin_and_accented_names_get_identity_and_company_keys():
    arabic = repo.lead_identity_keys(LeadIn(full_name="محمد العلي", lead_company="طيران الإمارات"), "person")
    assert arabic == ["nc:محمد العلي|n:طيرانالامارات"]
    assert repo.lead_identity_keys(LeadIn(lead_company="株式会社トヨタ"), "account") == ["acct:n:株式会社トヨタ"]
    assert repo.lead_identity_keys(LeadIn(full_name="Иван Петров", lead_company="Газпром"), "person") == [
        "nc:иван петров|n:газпром"]
    assert repo.company_key("Société Générale") == repo.company_key("Societe Generale") == "n:societegenerale"
    assert repo.company_key("مُحَمَّد") == repo.company_key("محمد")  # vowel points are optional in Arabic
    # Plain ASCII keys are unchanged, and symbols don't leak into keys through compatibility forms.
    assert repo.company_key("Acme Bank L.L.C.") == repo.company_key("Acme™ Bank") == "n:acmebank"
    assert repo.lead_identity_keys(LeadIn(full_name="Jane O'Brien-Smith", lead_company="Acme"), "person") == [
        "nc:jane o brien smith|n:acme"]


def test_non_latin_rows_dedupe_on_reimport_and_inherit_account_intent(company):
    rows = "Name,Company\nمحمد العلي,طيران الإمارات\nJohn Smith,Acme\n"
    assert import_leads_csv(company.id, rows)["created"] == 2
    again = import_leads_csv(company.id, rows)
    assert (again["created"], again["merged"]) == (0, 2)
    person, _ = repo.upsert_lead(company.id, LeadIn(full_name="田中 太郎", title="CFO", lead_company="株式会社トヨタ"))
    first = ingest(company.id, [_account_signal("株式会社トヨタ", "t1")])
    second = ingest(company.id, [_account_signal("株式会社トヨタ", "t2")])
    assert (first.leads_new, second.leads_new) == (1, 0)
    assert repo.get_lead(person.id).intent_score > 0


# --------------------------------------------------------------------------------------
# INT-01: namesakes at the same company
# --------------------------------------------------------------------------------------


def test_namesakes_with_different_strong_identities_stay_apart(company):
    a, _ = repo.upsert_lead(company.id, LeadIn(full_name="Mohammed Ali", lead_company="Emirates Group",
                                               linkedin_url="https://www.linkedin.com/in/mohammed-ali-123"))
    b, created = repo.upsert_lead(company.id, LeadIn(
        full_name="Mohammed Ali", lead_company="Emirates", linkedin_url="https://www.linkedin.com/in/mohammed-ali-987",
        email="m.ali987@example.com"))
    assert created and b.id != a.id
    assert repo.get_lead(a.id).email == "" and b.email == "m.ali987@example.com"
    assert "em:m.ali987@example.com" not in _keys(a.id)

    g1, _ = repo.upsert_lead(company.id, LeadIn(full_name="Alex Chen", lead_company="@google",
                                                github_username="alexchen"))
    g2, created = repo.upsert_lead(company.id, LeadIn(full_name="Alex Chen", lead_company="Google",
                                                      github_username="achen-ml"))
    assert created and g2.id != g1.id


def test_strong_key_beats_an_older_name_match(company):
    a, _ = repo.upsert_lead(company.id, LeadIn(full_name="Sara Khan", lead_company="Emaar"))
    b, _ = repo.upsert_lead(company.id, LeadIn(full_name="S. Khan", lead_company="Emaar",
                                               email="sara.k2@emaar.example"))
    assert a.id != b.id
    merged, created = repo.upsert_lead(company.id, LeadIn(full_name="Sara Khan", lead_company="Emaar", title="CFO",
                                                          email="sara.k2@emaar.example"))
    assert not created and merged.id == b.id and merged.title == "CFO"
    assert repo.get_lead(a.id).title == ""


def test_name_match_still_merges_when_identities_do_not_conflict(company):
    a, _ = repo.upsert_lead(company.id, LeadIn(full_name="Lina Haddad", lead_company="Acme",
                                               github_username="lina"))
    b, created = repo.upsert_lead(company.id, LeadIn(full_name="Lina Haddad", lead_company="Acme Ltd",
                                                     linkedin_url="https://linkedin.com/in/lina-h"))
    assert not created and b.id == a.id and b.linkedin_url == "https://linkedin.com/in/lina-h"


# --------------------------------------------------------------------------------------
# INT-03 / INT-04 / INT-05: account leads and the people who inherit their intent
# --------------------------------------------------------------------------------------


def test_account_turned_person_drops_account_keys(company):
    account, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Acme Bank", company_domain="acmebank.com"))
    jane = repo.update_lead(account.id, {"full_name": "Jane Roe", "title": "Head of Ops", "kind": "person"})
    assert jane.kind == "person" and not [k for k in _keys(jane.id) if k.startswith("acct:")]
    bob, _ = repo.upsert_lead(company.id, LeadIn(full_name="Bob", title="CFO", lead_company="Acme Bank"))
    stats = ingest(company.id, [_account_signal("Acme Bank", "f1")])
    assert stats.leads_new == 1
    assert repo.get_lead(bob.id).intent_score > 0
    # Naming an account's contact without passing kind makes it a person too, like upsert_lead does.
    other, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Globex"))
    assert repo.update_lead(other.id, {"full_name": "Ann Lee", "title": "CEO"}).kind == "person"
    # And a person turned into an account loses its personal keys.
    back = repo.update_lead(jane.id, {"kind": "account", "full_name": ""})
    assert back.kind == "account" and all(k.startswith("acct:") for k in _keys(back.id))


def test_deleting_or_renaming_an_account_rescores_its_people(company):
    account, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Globex"))
    for ext in ("s1", "s2"):
        repo.add_signal(company.id, SignalIn(type="funding", title=f"Globex {ext}", strength=100, external_id=ext),
                        lead_id=account.id)
    carl, _ = repo.upsert_lead(company.id, LeadIn(full_name="Carl", title="CEO", lead_company="Globex"))
    assert repo.get_lead(carl.id).intent_score > 0
    repo.delete_lead(account.id)
    after = repo.get_lead(carl.id)
    assert after.intent_score == 0 and after.last_signal_at is None

    initech, _ = repo.upsert_lead(company.id, LeadIn(lead_company="Initech"))
    repo.add_signal(company.id, SignalIn(type="funding", title="Initech raised", strength=100), lead_id=initech.id)
    dan, _ = repo.upsert_lead(company.id, LeadIn(full_name="Dan", title="CEO", lead_company="Initech"))
    assert repo.get_lead(dan.id).intent_score > 0
    repo.update_lead(initech.id, {"lead_company": "Initech Logistics"})
    assert repo.get_lead(dan.id).intent_score == 0


def test_domain_only_account_moves_to_name_key_when_merge_adds_the_name(company):
    account, _ = repo.upsert_lead(company.id, LeadIn(company_domain="https://www.acmebank.com"))
    repo.add_signal(company.id, SignalIn(type="funding", title="Acme Bank raised", strength=100), lead_id=account.id)
    stats = ingest(company.id, [_account_signal("Acme Bank", "n1", domain="acmebank.com", strength=50)])
    assert (stats.leads_new, stats.leads_updated) == (0, 1)
    cfo, _ = repo.upsert_lead(company.id, LeadIn(full_name="Cathy", title="CFO", lead_company="Acme Bank"))
    assert [p.id for p in repo.contacts_at_account(repo.get_lead(account.id))] == [cfo.id]
    assert repo.get_lead(cfo.id).intent_score > 0
    # The merged account now also answers to its name alone.
    again = ingest(company.id, [_account_signal("Acme Bank", "n2")])
    assert again.leads_new == 0


def test_domain_only_people_are_rescored_when_their_account_moves_to_the_name_key(company):
    account, _ = repo.upsert_lead(company.id, LeadIn(company_domain="initech.com"))
    repo.add_signal(company.id, SignalIn(type="funding", title="raised", strength=100), lead_id=account.id)
    dora, _ = repo.upsert_lead(company.id, LeadIn(full_name="Dora", title="CFO", company_domain="initech.com"))
    assert repo.get_lead(dora.id).intent_score > 0
    ingest(company.id, [_account_signal("Initech", "x1", domain="initech.com")])
    # Dora's key is the domain, the account's is now the name: her score must not keep stale intent.
    dora_after = repo.get_lead(dora.id)
    assert dora_after.intent_score == 0 and dora_after.last_signal_at is None


def test_update_lead_null_notes_and_tags_clear_them(company):
    lead, _ = repo.upsert_lead(company.id, LeadIn(full_name="Nia", notes="call back", tags=["vip"]))
    cleared = repo.update_lead(lead.id, {"notes": None, "tags": None})
    assert cleared.notes == "" and cleared.tags == []


def test_refresh_identity_keys_rekeys_rows_written_by_older_versions(company):
    from openberry.db import connect

    person, _ = repo.upsert_lead(company.id, LeadIn(full_name="محمد العلي", lead_company="طيران الإمارات"))
    with connect() as c:  # what a database written before Unicode-aware keys looks like
        c.execute("UPDATE leads SET company_key = '' WHERE id = ?", (person.id,))
        c.execute("DELETE FROM lead_keys WHERE lead_id = ?", (person.id,))
    assert repo.refresh_identity_keys() == 1
    assert _keys(person.id) == ["nc:محمد العلي|n:طيرانالامارات"]
    _, created = repo.upsert_lead(company.id, LeadIn(full_name="محمد العلي", lead_company="طيران الإمارات"))
    assert not created


def test_opening_a_version_1_database_rekeys_its_leads(settings):
    """Databases written before Unicode-aware keys are re-keyed once, when the app first opens them."""
    import re
    import sqlite3

    from openberry import db

    conn = sqlite3.connect(settings.db_path)
    conn.executescript(re.sub(r"\n\s*alerted_at TEXT,[^\n]*", "", db.SCHEMA))  # the version 1 tables
    now = repo.iso()
    conn.execute("INSERT INTO companies (name, created_at, updated_at) VALUES ('Old Co', ?, ?)", (now, now))
    conn.executemany(  # what version 1 stored: no key at all for Arabic, an accent-less key for French
        "INSERT INTO leads (company_id, kind, full_name, lead_company, company_key, created_at, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        [("person", "محمد العلي", "طيران الإمارات", "", now, now),
         ("account", "", "Société Générale", "n:socitgnrale", now, now)])
    conn.execute("INSERT INTO lead_keys (company_id, key, lead_id) VALUES (1, 'acct:n:socitgnrale', 2)")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    assert repo.upsert_lead(1, LeadIn(full_name="محمد العلي", lead_company="طيران الإمارات"))[0].id == 1
    assert repo.upsert_lead(1, LeadIn(lead_company="Societe Generale"))[0].id == 2
    assert repo.list_leads(1)[1] == 2
    with db.connect() as c:
        assert c.execute("SELECT company_key FROM leads WHERE id = 2").fetchone()[0] == "n:societegenerale"
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# --------------------------------------------------------------------------------------
# CSV: J1, INT-06, J2, J9
# --------------------------------------------------------------------------------------


def _profiles(company_id: int) -> set[tuple[str, ...]]:
    fields = ("full_name", "title", "lead_company", "location", "linkedin_url", "email", "phone", "kind")
    return {tuple(getattr(lead, f) for f in fields) for lead in repo.list_leads(company_id, limit=5000)[0]}


def test_export_reimports_without_shifting_columns():
    from openberry.seed import seed_demo

    demo_id = seed_demo()
    repo.upsert_lead(demo_id, LeadIn(
        full_name="Priya Nair", title="Event Manager", lead_company="Events, Inc. Dubai", location="Dubai, UAE",
        linkedin_url="https://www.linkedin.com/in/priya-nair-events", email="priya@events.example",
        phone="+971 50 123 4567"))
    exported = export_leads_csv(repo.list_leads(demo_id, limit=5000)[0])
    # Cells like "'+ Title matches 'Executive Assistant' | ..." made csv.Sniffer guess quotechar="'".
    assert "'+ Title matches '" in exported
    other = repo.create_company(CompanyIn(name="Second Co"))
    stats = import_leads_csv(other.id, exported)
    assert stats["errors"] == [] and stats["created"] == len(_profiles(demo_id))
    assert _profiles(other.id) == _profiles(demo_id)


def test_export_reimport_merges_collector_leads_and_strips_formula_escape(company):
    repo.upsert_lead(company.id, LeadIn(full_name="pgfan", profile_url="https://news.ycombinator.com/user?id=pgfan",
                                        source="hackernews"))
    repo.upsert_lead(company.id, LeadIn(full_name="redditor42", profile_url="https://www.reddit.com/user/redditor42",
                                        source="reddit"))
    repo.upsert_lead(company.id, LeadIn(full_name="Xavier", twitter="@xav", phone="+971 50 123 4567"))
    exported = export_leads_csv(repo.list_leads(company.id)[0])
    stats = import_leads_csv(company.id, exported)
    assert (stats["created"], stats["merged"]) == (0, 3)
    other = repo.create_company(CompanyIn(name="Second Co"))
    import_leads_csv(other.id, exported)
    copies = {lead.full_name: lead for lead in repo.list_leads(other.id)[0]}
    assert copies["Xavier"].twitter == "@xav" and copies["Xavier"].phone == "+971 50 123 4567"
    assert copies["pgfan"].profile_url == "https://news.ycombinator.com/user?id=pgfan"


def test_profile_url_header_is_not_assumed_to_be_linkedin(company):
    import_leads_csv(company.id, "Name,Profile URL\nAnn,https://github.com/ann\nBea,https://www.linkedin.com/in/bea\n")
    leads = {lead.full_name: lead for lead in repo.list_leads(company.id)[0]}
    assert leads["Ann"].linkedin_url == "" and leads["Ann"].profile_url == "https://github.com/ann"
    assert leads["Bea"].linkedin_url == "https://www.linkedin.com/in/bea"


LINKEDIN_CONNECTIONS = (
    "Notes:\r\n"
    '"When exporting your connection data, you may notice that some of the email addresses are missing. '
    'You will only see email addresses for connections who have allowed their connections to see or download '
    'their email address using this setting https://www.linkedin.com/psettings/privacy/email. You can learn more '
    'about this here: https://www.linkedin.com/help/linkedin/answer/261"\r\n'
    "\r\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\r\n"
    "Aisha,Rahman,https://www.linkedin.com/in/aisha-rahman,,\"Harbor Lane Bank, PJSC\",Executive Assistant,"
    "08 Oct 2026\r\n"
    "Omar,Haddad,https://www.linkedin.com/in/omar-haddad,omar@northwind.example,Northwind,Travel Manager,"
    "01 Sep 2026\r\n"
    ",,,,,,\r\n"
)


def test_linkedin_connections_export_skips_the_notes_preamble(company):
    stats = import_leads_csv(company.id, LINKEDIN_CONNECTIONS)
    assert (stats["created"], stats["skipped"]) == (2, 1)
    leads = {lead.full_name: lead for lead in repo.list_leads(company.id)[0]}
    aisha, omar = leads["Aisha Rahman"], leads["Omar Haddad"]
    assert aisha.linkedin_url == "https://www.linkedin.com/in/aisha-rahman" and aisha.title == "Executive Assistant"
    assert aisha.lead_company == "Harbor Lane Bank, PJSC"
    assert omar.email == "omar@northwind.example" and omar.title == "Travel Manager"


def test_import_without_a_name_or_company_column_is_rejected(company):
    with pytest.raises(ValueError, match="no recognisable columns"):
        import_leads_csv(company.id, "Notes:\nsome text\n\nEmail,Phone\na@b.example,123\n")


def test_semicolon_and_tab_separated_files(company):
    import_leads_csv(company.id, "Nom;Name;Company\nx;Jo Lee;Acme\n")
    import_leads_csv(company.id, "Name\tTitle\nKim Park\tCFO\n")
    names = {lead.full_name: lead for lead in repo.list_leads(company.id)[0]}
    assert names["Jo Lee"].lead_company == "Acme" and names["Kim Park"].title == "CFO"


def test_apollo_export_columns_map_to_the_right_fields(company):
    csv_text = (
        "First Name,Last Name,Title,Company,Company Name for Emails,Email,Email Status,Seniority,First Phone,"
        "Work Direct Phone,Mobile Phone,Corporate Phone,# Employees,Industry,Person Linkedin Url,Website,"
        "Company Linkedin Url,Twitter Url,City,State,Country\n"
        "Layla,Saeed,CFO,Gulf Capital,Gulf Capital Partners,layla@gulfcap.example,Verified,C suite,,,"
        "+971 50 000 0000,+971 4 000 0000,120,Financial Services,http://www.linkedin.com/in/layla-saeed,"
        "http://www.gulfcap.example,http://www.linkedin.com/company/gulf-capital,https://twitter.com/laylas,Dubai,"
        "Dubai,United Arab Emirates\n"
    )
    assert import_leads_csv(company.id, csv_text)["created"] == 1
    lead = repo.list_leads(company.id)[0][0]
    assert lead.linkedin_url == "http://www.linkedin.com/in/layla-saeed"
    assert lead.phone == "+971 50 000 0000" and lead.twitter == "https://twitter.com/laylas"
    assert (lead.lead_company, lead.company_size, lead.email) == ("Gulf Capital", "120", "layla@gulfcap.example")
    assert lead.location == "Dubai, United Arab Emirates"
