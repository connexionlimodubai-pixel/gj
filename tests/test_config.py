"""The .env loader and the shipped .env.example."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from openberry import config
from openberry.collectors import reddit

ROOT = Path(__file__).resolve().parents[1]
ENV_LINE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$")
KEYS = ("OB_TEST_PLAIN", "OB_TEST_COMMENT", "OB_TEST_TAB", "OB_TEST_HASH", "OB_TEST_EMPTY",
        "OB_TEST_DQ", "OB_TEST_SQ", "OB_TEST_SPACED", "OB_TEST_WINS", "REDDIT_USERNAME")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keys these tests load start unset and are removed again afterwards."""
    for key in KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def test_inline_comments_are_stripped_from_unquoted_values_only(tmp_path: Path, clean_env: None,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OB_TEST_WINS", "from the environment")
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "# a comment line",
        "OB_TEST_PLAIN=plain",
        "OB_TEST_COMMENT=myname          # your Reddit username",
        "OB_TEST_TAB=value\t# tab before the comment",
        "OB_TEST_HASH=pa#ss",
        "OB_TEST_EMPTY=          # nothing set yet",
        'OB_TEST_DQ="a # b"  # the quoted part is the value',
        "OB_TEST_SQ='#not a comment'",
        "OB_TEST_SPACED = spaced value ",
        "OB_TEST_WINS=from the file",
    ]), encoding="utf-8")

    config._load_dotenv(env)

    assert os.environ["OB_TEST_PLAIN"] == "plain"
    assert os.environ["OB_TEST_COMMENT"] == "myname"
    assert os.environ["OB_TEST_TAB"] == "value"
    assert os.environ["OB_TEST_HASH"] == "pa#ss"
    assert os.environ["OB_TEST_EMPTY"] == ""
    assert os.environ["OB_TEST_DQ"] == "a # b"
    assert os.environ["OB_TEST_SQ"] == "#not a comment"
    assert os.environ["OB_TEST_SPACED"] == "spaced value"
    assert os.environ["OB_TEST_WINS"] == "from the environment"


def test_reddit_username_with_a_trailing_comment_reaches_the_user_agent(tmp_path: Path, clean_env: None,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "openberry.env"
    env.write_text("REDDIT_USERNAME=myname          # sent in the User-Agent\n", encoding="utf-8")
    monkeypatch.setenv("OPENBERRY_ENV_FILE", str(env))

    settings = config.Settings.from_env()

    assert settings.reddit_username == "myname"
    assert "by /u/myname" in reddit.user_agent(settings)


def test_env_example_values_have_no_inline_comments_and_are_all_read() -> None:
    """Every `KEY=value` line of .env.example, commented out or not, works when uncommented as is."""
    source = (ROOT / "src/openberry/config.py").read_text(encoding="utf-8")
    entries = [m.groups() for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
               if (m := ENV_LINE.match(line))]
    assert entries
    for key, value in entries:
        assert "#" not in value, f"{key}: put the comment on its own line"
        assert f'"{key}"' in source, f"{key} is not read by config.py"
        assert key not in {"OPENBERRY_HOME", "OPENBERRY_ENV_FILE"}, f"{key} is read before any .env file"
