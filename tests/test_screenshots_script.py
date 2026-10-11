"""scripts/screenshots.py, the developers' tool behind docs/screenshots: it must never touch real data or the network.

The photographing itself needs Chromium and a minute, so it isn't run here; everything that keeps it safe is.
"""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from openberry import repo
from sample_data import seed_sample_company

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "screenshots.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("openberry_scripts_screenshots", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shots = _load()


def test_it_refuses_a_data_folder_that_holds_anything(tmp_path: Path) -> None:
    with pytest.raises(shots.Refused, match="set OPENBERRY_HOME"):
        shots.data_folder({})
    used = tmp_path / "used"
    used.mkdir()
    (used / "openberry.db").write_bytes(b"real data")
    with pytest.raises(shots.Refused, match="is not empty"):
        shots.data_folder({"OPENBERRY_HOME": str(used)})
    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(shots.Refused, match="is not empty"):
        shots.data_folder({"OPENBERRY_HOME": str(a_file)})
    assert shots.data_folder({"OPENBERRY_HOME": str(tmp_path / "new")}) == (tmp_path / "new").resolve()
    assert (tmp_path / "new").is_dir()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert shots.data_folder({"OPENBERRY_HOME": str(empty)}) == empty.resolve()


def test_main_stops_before_anything_on_a_used_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "openberry.db").write_bytes(b"real data")
    monkeypatch.setenv("OPENBERRY_HOME", str(tmp_path))
    assert shots.main(["--out", str(tmp_path / "out")]) == 2
    assert "is not empty" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["openberry.db"]  # nothing written, not even --out


def test_the_server_gets_no_settings_keys_or_proxies(tmp_path: Path) -> None:
    environ = {"PATH": "/usr/bin", "HOME": "/home/me", "OPENBERRY_SMTP_PASSWORD": "secret",
               "OPENBERRY_GOOGLE_PLACES_KEY": "AIza", "OPENBERRY_DB": "/home/me/.openberry/openberry.db",
               "OPENBERRY_ENV_FILE": "/home/me/.env", "GITHUB_TOKEN": "ghp", "REDDIT_CLIENT_SECRET": "r",
               "HTTPS_PROXY": "http://proxy:3128", "OPENBERRY_PASSWORD": "pw"}
    env = shots.clean_env(environ, tmp_path, "http://127.0.0.1:9999")
    assert env["OPENBERRY_HOME"] == str(tmp_path) and env["OPENBERRY_DB"] == str(tmp_path / "openberry.db")
    assert env["OPENBERRY_ENV_FILE"] == str(tmp_path / ".env")  # the only .env read: not ./.env or ~/.openberry/.env
    assert env["OPENBERRY_BASE_URL"] == "http://127.0.0.1:9999"
    assert {"PATH", "HOME"} <= set(env)
    for key in ("OPENBERRY_SMTP_PASSWORD", "OPENBERRY_GOOGLE_PLACES_KEY", "GITHUB_TOKEN", "REDDIT_CLIENT_SECRET",
                "HTTPS_PROXY", "OPENBERRY_PASSWORD"):
        assert key not in env, key


def run_guarded(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", shots.NO_NETWORK + code], capture_output=True, text=True,
                          timeout=60)


def test_the_guard_refuses_every_address_but_this_computer() -> None:
    for code in ("socket.create_connection(('203.0.113.7', 587), timeout=2)",
                 "socket.getaddrinfo('smtp.gmail.com', 465)",
                 "socket.getaddrinfo(b'places.googleapis.com', 443)",
                 "socket.socket().connect_ex(('203.0.113.7', 993))"):
        result = run_guarded("import socket\n" + code)
        assert result.returncode == 1 and "allows no network" in result.stderr, (code, result.stderr)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        result = run_guarded(f"import socket\nsocket.create_connection(('127.0.0.1', {port}), timeout=5).close()\n"
                             f"socket.getaddrinfo('localhost', {port})")
    assert result.returncode == 0, result.stderr


def test_the_sample_company_is_not_the_old_demo_and_sends_nothing() -> None:
    company_id = seed_sample_company(name=shots.COMPANY, status="paused", brand=shots.BRAND)
    company = repo.get_company(company_id)
    assert company.name == shots.COMPANY and company.status == "paused"
    assert not (company.outreach.email_sending or company.outreach.agent_sending or company.outreach.auto_approve)
    text = company.model_dump_json() + "".join(
        s.model_dump_json() for s in repo.list_signals(company_id, limit=500)[0])
    assert "Desert Line" not in text and "demo" not in text.lower() and shots.BRAND in text
    assert repo.companies_due_for_scan() == []  # paused: the server it starts never scans


def test_it_photographs_the_pages_the_readme_shows() -> None:
    names = [name for name, _, _ in shots.pages({"company": 1, "lead": 1})]
    assert {"dashboard.png", "registration-board.png", "register.png", "lead.png", "outreach.png",
            "mailbox.png"} == set(names)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in names:
        assert f"(docs/screenshots/{name})" in readme, name
        assert (ROOT / "docs" / "screenshots" / name).is_file(), name


def test_the_script_ships_nowhere() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/openberry"]
    assert "scripts" in (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert not (ROOT / "src" / "openberry" / "seed.py").exists()
