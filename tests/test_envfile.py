"""Settings the dashboard saves to the data folder's .env file (envfile.py) and config.py reading them back."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from openberry import config, envfile
from openberry.config import get_settings

KEY = "OPENBERRY_GOOGLE_PLACES_KEY"
LIMIT = "OPENBERRY_GOOGLE_PLACES_MONTHLY_LIMIT"
GOOGLE_KEY = "AIzaSyD-test_key_0123456789abcdefghi4f2c"  # 39 characters, like a real key
POSIX = sys.platform != "win32"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data folder read the way a real install reads it: ./.env first, then OPENBERRY_HOME/.env."""
    data = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.delenv("OPENBERRY_ENV_FILE")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(config, "OPENBERRY_HOME", data)
    monkeypatch.setattr(config, "_FROM_FILE", {})
    monkeypatch.setattr(config, "_settings_file_seen", None)
    for name in config.DASHBOARD_SETTINGS:  # unset now, and removed again after the test
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return data


def env_text(home: Path) -> str:
    return (home / ".env").read_text(encoding="utf-8")


def test_save_creates_a_private_file_and_the_key_is_live_at_once(home: Path) -> None:
    settings = get_settings()
    secret_before = settings.secret_key

    path = envfile.save_settings({KEY: GOOGLE_KEY})

    assert path == home / ".env" and path.is_file()
    assert env_text(home) == f'{KEY}="{GOOGLE_KEY}"\n'
    if POSIX:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert os.environ[KEY] == GOOGLE_KEY
    assert get_settings() is settings  # the same object: app.state.settings sees it too
    assert settings.google_places_key == GOOGLE_KEY
    assert settings.secret_key == secret_before  # logins survive
    assert config.setting_source(KEY) == ("file", path.resolve())


@pytest.mark.skipif(not POSIX, reason="file modes")
def test_saving_tightens_an_existing_world_readable_file(home: Path) -> None:
    home.mkdir()
    (home / ".env").write_text("OTHER=1\n", encoding="utf-8")
    os.chmod(home / ".env", 0o644)
    envfile.save_settings({LIMIT: "500"})
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600


def test_other_lines_and_comments_are_kept_and_the_line_is_replaced_in_place(home: Path) -> None:
    home.mkdir()
    original = ("# my settings\n"
                "OPENBERRY_PASSWORD=pa#ss   # keep me\n"
                f"  {KEY} = old-key-value-0000000000\n"
                "\n"
                "# Google Maps\n"
                f"{KEY}=duplicate-later-line\n"
                "GITHUB_TOKEN='abc'\n")
    (home / ".env").write_text(original, encoding="utf-8")

    envfile.save_settings({KEY: GOOGLE_KEY, LIMIT: "250"})

    assert env_text(home) == ("# my settings\n"
                              "OPENBERRY_PASSWORD=pa#ss   # keep me\n"
                              f'{KEY}="{GOOGLE_KEY}"\n'
                              "\n"
                              "# Google Maps\n"
                              "GITHUB_TOKEN='abc'\n"
                              f'{LIMIT}="250"\n')
    assert get_settings().google_places_monthly_limit == 250


@pytest.mark.parametrize("removal", [None, "", "   "])
def test_none_or_empty_removes_the_setting(home: Path, removal: str | None) -> None:
    envfile.save_settings({KEY: GOOGLE_KEY, LIMIT: "300"})
    envfile.save_settings({KEY: removal})
    assert KEY not in env_text(home) and f'{LIMIT}="300"' in env_text(home)
    assert KEY not in os.environ
    assert get_settings().google_places_key == ""
    assert config.setting_source(KEY) == ("", None)
    envfile.save_settings({LIMIT: None})
    assert get_settings().google_places_monthly_limit == config.DEFAULT_GOOGLE_PLACES_MONTHLY_LIMIT


@pytest.mark.parametrize("value", ["a # b", "it's mine", 'say "hi"', "  padded  ", "x=y;z"])
def test_values_round_trip_through_the_dotenv_reader(home: Path, value: str) -> None:
    path = envfile.save_settings({KEY: value})
    assert config.read_dotenv(path)[KEY] == value.strip()
    assert get_settings().google_places_key == value.strip()


@pytest.mark.parametrize("value", ["two\nlines", "cr\rhere", "nul\0", "both ' and \""])
def test_values_a_dotenv_file_cannot_hold_are_refused(home: Path, value: str) -> None:
    with pytest.raises(envfile.SettingNotSaved, match="line breaks or quotes"):
        envfile.save_settings({KEY: value})
    assert not (home / ".env").exists()


def test_only_dashboard_settings_can_be_saved(home: Path) -> None:
    with pytest.raises(envfile.SettingNotSaved, match="can't be changed from the dashboard"):
        envfile.save_settings({"OPENBERRY_PASSWORD": "x"})
    with pytest.raises(envfile.SettingNotSaved):
        envfile.save_settings({KEY: GOOGLE_KEY, "OPENBERRY_DB": "/tmp/other.db"})
    assert not (home / ".env").exists()  # nothing is written when one name is refused


def test_a_real_environment_variable_wins_and_is_never_overwritten(home: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY, "from-the-server-environment-000")
    with pytest.raises(envfile.SettingNotSaved, match="server's environment"):
        envfile.save_settings({KEY: GOOGLE_KEY})
    assert not (home / ".env").exists()
    state = envfile.setting_state(KEY)
    assert (state.source, state.is_set) == ("environment", True)
    # Another process saving to the file doesn't change it either.
    home.mkdir()
    (home / ".env").write_text(f"{KEY}={GOOGLE_KEY}\n", encoding="utf-8")
    config.refresh_saved_settings()
    assert os.environ[KEY] == "from-the-server-environment-000"


def test_a_dotenv_file_read_first_is_named(home: Path) -> None:
    Path(".env").write_text(f"{KEY}=from-the-working-folder-000000\n", encoding="utf-8")
    config.Settings.from_env()
    with pytest.raises(envfile.SettingNotSaved, match="which OpenBerry reads first") as err:
        envfile.save_settings({KEY: GOOGLE_KEY})
    assert str(Path(".env").resolve()) in str(err.value)
    state = envfile.setting_state(KEY)
    assert state.source == "file" and state.where == str(Path(".env").resolve())


def test_an_empty_variable_does_not_hide_a_saved_key(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """docker compose passes `KEY=` lines of its env_file on as empty environment variables."""
    monkeypatch.setenv(KEY, "")
    home.mkdir()
    (home / ".env").write_text(f'{KEY}="{GOOGLE_KEY}"\n', encoding="utf-8")
    assert config.Settings.from_env().google_places_key == GOOGLE_KEY
    assert envfile.setting_state(KEY).source == "saved"


def test_openberry_env_file_is_the_file_written(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "conf" / "openberry.env"
    monkeypatch.setenv("OPENBERRY_ENV_FILE", str(custom))
    assert config.settings_file() == custom
    envfile.save_settings({KEY: GOOGLE_KEY})
    assert custom.read_text(encoding="utf-8") == f'{KEY}="{GOOGLE_KEY}"\n'
    assert not (home / ".env").exists()


def test_refresh_applies_what_another_process_saved(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    config.refresh_saved_settings()  # nothing there yet
    home.mkdir()
    (home / ".env").write_text(f'{KEY}="{GOOGLE_KEY}"\n{LIMIT}=120\n', encoding="utf-8")

    assert config.refresh_saved_settings() is True
    assert (settings.google_places_key, settings.google_places_monthly_limit) == (GOOGLE_KEY, 120)

    reads: list[Path] = []
    real_read = config.read_dotenv
    monkeypatch.setattr(config, "read_dotenv", lambda path: reads.append(path) or real_read(path))
    assert config.refresh_saved_settings() is False
    assert reads == []  # unchanged file: one stat() call, no parsing

    (home / ".env").write_text("# the key was removed\n", encoding="utf-8")
    assert config.refresh_saved_settings() is True
    assert settings.google_places_key == "" and KEY not in os.environ
    assert settings.google_places_monthly_limit == config.DEFAULT_GOOGLE_PLACES_MONTHLY_LIMIT


def test_setting_state_never_shows_a_secret(home: Path) -> None:
    assert envfile.setting_state(KEY) == envfile.SettingState(KEY, False, "", "", "")
    envfile.save_settings({KEY: GOOGLE_KEY, LIMIT: "750"})
    state = envfile.setting_state(KEY)
    assert (state.is_set, state.source, state.hint) == (True, "saved", "ending in 4f2c")
    assert GOOGLE_KEY not in repr(state)
    assert envfile.setting_state(LIMIT).hint == "750"  # not a secret
    envfile.save_settings({KEY: "short-key"})
    assert envfile.setting_state(KEY).hint == ""  # four characters of a short key would give too much away
    assert envfile.secret_hint("abcdefghijk") == "" and envfile.secret_hint("abcdefghijkl") == "ending in ijkl"


def test_a_failed_replace_keeps_the_old_file_and_leaves_no_temp_file(home: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    envfile.save_settings({LIMIT: "100"})
    before = env_text(home)

    def broken_replace(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(envfile.os, "replace", broken_replace)
    with pytest.raises(OSError):
        envfile.save_settings({KEY: GOOGLE_KEY})
    assert env_text(home) == before
    assert sorted(p.name for p in home.iterdir()) == [".env"]
    assert get_settings().google_places_key == ""  # nothing was applied either


def test_windows_sharing_violations_are_retried(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace
    calls: list[int] = []

    def flaky_replace(src: object, dst: object) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(envfile.os, "replace", flaky_replace)
    monkeypatch.setattr(envfile.time, "sleep", lambda seconds: None)
    envfile.save_settings({KEY: GOOGLE_KEY})
    assert len(calls) == 3 and GOOGLE_KEY in env_text(home)


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_a_symlinked_env_file_stays_a_link(home: Path, tmp_path: Path) -> None:
    target = tmp_path / "dotfiles" / "openberry.env"
    target.parent.mkdir()
    target.write_text("# shared\n", encoding="utf-8")
    home.mkdir()
    (home / ".env").symlink_to(target)

    envfile.save_settings({KEY: GOOGLE_KEY})

    assert (home / ".env").is_symlink()
    assert target.read_text(encoding="utf-8") == f'# shared\n{KEY}="{GOOGLE_KEY}"\n'


def test_settings_repr_hides_the_key() -> None:
    settings = config.Settings(google_places_key=GOOGLE_KEY)
    assert GOOGLE_KEY not in repr(settings)


@pytest.mark.parametrize(("raw", "expected"), [("250", 250), ("-5", 0), ("abc", 900), ("", 900)])
def test_monthly_limit_from_the_environment(home: Path, monkeypatch: pytest.MonkeyPatch, raw: str,
                                            expected: int) -> None:
    monkeypatch.setenv(LIMIT, raw)
    assert config.Settings.from_env().google_places_monthly_limit == expected
