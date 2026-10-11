"""Shared fixtures: every test gets its own temporary SQLite database and default settings."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from email_fakes import offline_mail  # noqa: F401  (a fixture: fake mail servers, no network)
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


@pytest.fixture(autouse=True)
def _keep_umask():
    """cli.main and desktop.main set a private umask (config.private_files) for the whole process: undo it."""
    umask = os.umask(0o022)
    os.umask(umask)
    yield
    os.umask(umask)


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


@pytest.fixture
def sample_company() -> int:
    """The test-only sample company (tests/sample_data.py): 10 people, 3 accounts, signals and 2 drafts. Its id."""
    from sample_data import seed_sample_company

    return seed_sample_company()


# --------------------------------------------------------------------------------------
# Real-browser tests (test_*_browser.py): the dashboard in a thread, and a Chromium for Playwright
# --------------------------------------------------------------------------------------

@pytest.fixture
def base_url(settings) -> Iterator[str]:
    import uvicorn

    from openberry.web.app import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


@pytest.fixture
def browser() -> Iterator[Any]:
    """Playwright's own Chromium, else any Chromium in PLAYWRIGHT_BROWSERS_PATH; skipped without one."""
    sync_api = pytest.importorskip("playwright.sync_api")
    candidates: list[str | None] = [None]
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root:
        candidates += sorted(str(p) for p in Path(root).glob("chromium-*/chrome-*/chrome"))
    with sync_api.sync_playwright() as pw:
        found = None
        for executable in candidates:
            try:
                found = pw.chromium.launch(executable_path=executable)
                break
            except sync_api.Error:
                continue
        if found is None:
            pytest.skip("no Chromium for Playwright")
        try:
            yield found
        finally:
            found.close()
