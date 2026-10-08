"""Shared fixtures: every test gets its own temporary SQLite database and default settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from openberry import db
from openberry.config import Settings, set_settings


@pytest.fixture(autouse=True)
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPENBERRY_ENV_FILE", str(tmp_path / "missing.env"))
    s = Settings(db_path=tmp_path / "test.db", secret_key="test-secret", scheduler_enabled=False)
    set_settings(s)
    db.reset_init_cache()
    yield s
    db.reset_init_cache()


@pytest.fixture
def company():
    """A registered company with a small but complete profile."""
    from openberry import repo
    from openberry.models import CompanyIn

    return repo.create_company(CompanyIn.model_validate({
        "name": "Acme Chauffeurs",
        "website": "https://acme.example",
        "description": "Executive chauffeur service",
        "value_proposition": "Reliable corporate transport with monthly invoicing",
        "competitors": ["Blacklane", "Careem Business"],
        "icp": {
            "job_titles": ["Executive Assistant", "Travel Manager"],
            "seniorities": ["manager", "director"],
            "industries": ["Finance", "Consulting"],
            "company_sizes": ["51-200", "201-1000"],
            "locations": ["UAE"],
            "keywords": ["corporate travel"],
            "exclude_keywords": ["student"],
        },
        "signals": {
            "keywords": ["chauffeur", "corporate travel"],
            "subreddits": ["dubai"],
            "github_repos": ["acme/limo-sdk"],
            "job_boards": ["greenhouse:acmebank:Acme Bank", "lever:northwind:Northwind"],
            "hiring_keywords": ["Executive Assistant", "Travel"],
            "news_queries": ["Dubai office opening"],
            "rss_feeds": ["https://feeds.example.com/news.xml"],
        },
        "outreach": {"sender_name": "Sam", "calendar_link": "https://cal.example/sam"},
    }))
