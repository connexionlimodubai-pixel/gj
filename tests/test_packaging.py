"""The desktop app's packaging: the PyInstaller spec, launcher, build script, smoke test, CI workflow and docs.

Building runs PyInstaller for a minute or more, so it happens in CI (.github/workflows/desktop.yml) and with
`python packaging/build.py`. These tests check everything else, on any machine. The smoke test runs for real
against the source app (`python -c 'openberry.desktop.main()'`), which the packaged executables also run.
"""

from __future__ import annotations

import functools
import importlib.util
import io
import os
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import openberry
from openberry.web import pages

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop.yml"
posix_only = pytest.mark.skipif(os.name == "nt", reason="fake executables are shell scripts")


def _load(name: str) -> ModuleType:
    """Import packaging/<name>.py by path: `packaging` would otherwise be the PyPI package of that name."""
    spec = importlib.util.spec_from_file_location(f"openberry_packaging_{name}", PACKAGING / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class is created
    spec.loader.exec_module(module)
    return module


launcher = _load("launcher")
build = _load("build")
smoke = _load("smoke_test")
docker_check = _load("docker_check")


def script(path: Path, body: str) -> Path:
    """An executable shell script at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def source_app(path: Path) -> Path:
    """An executable that runs the source app the way the packaged executables do (launcher -> desktop.main)."""
    return script(path, f'exec "{sys.executable}" -c "from openberry.desktop import main; main()" "$@"')


# ======================================================================================
# launcher.py
# ======================================================================================

def test_restore_library_path_gives_children_the_original_value() -> None:
    env = {"LD_LIBRARY_PATH": "/app/_internal:/opt/lib", "LD_LIBRARY_PATH_ORIG": "/opt/lib", "HOME": "/home/me"}
    launcher.restore_library_path(env, frozen=True, platform="linux")
    assert env == {"LD_LIBRARY_PATH": "/opt/lib", "HOME": "/home/me"}


def test_restore_library_path_unsets_what_was_unset_before() -> None:
    env = {"LD_LIBRARY_PATH": "/app/_internal"}
    launcher.restore_library_path(env, frozen=True, platform="linux")
    assert env == {}


@pytest.mark.parametrize(("frozen", "platform"), [(False, "linux"), (True, "darwin"), (True, "win32")])
def test_restore_library_path_leaves_other_cases_alone(frozen: bool, platform: str) -> None:
    env = {"LD_LIBRARY_PATH": "/app/_internal:/opt/lib", "LD_LIBRARY_PATH_ORIG": "/opt/lib"}
    launcher.restore_library_path(env, frozen=frozen, platform=platform)
    assert env == {"LD_LIBRARY_PATH": "/app/_internal:/opt/lib", "LD_LIBRARY_PATH_ORIG": "/opt/lib"}


def test_restore_library_path_defaults_to_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    launcher.restore_library_path()
    assert os.environ["LD_LIBRARY_PATH"] == "/usr/local/lib" and "LD_LIBRARY_PATH_ORIG" not in os.environ


def test_launcher_main_restores_the_environment_then_runs_the_desktop_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(launcher, "restore_library_path", lambda: calls.append("restore"))
    monkeypatch.setattr("openberry.desktop.main", lambda: calls.append("desktop.main"))
    launcher.main()
    assert calls == ["restore", "desktop.main"]


# ======================================================================================
# openberry.spec (executed with stand-ins for PyInstaller's classes)
# ======================================================================================

class Recorder:
    """Stands in for one of PyInstaller's build classes: records how the spec calls it."""

    def __init__(self, kind: str, calls: list[Recorder], *args: Any, **kwargs: Any) -> None:
        self.kind, self.args, self.kwargs = kind, args, kwargs
        self.pure: list[Any] = []
        self.scripts: list[Any] = []
        self.binaries: list[Any] = []
        self.datas: list[Any] = list(kwargs.get("datas", []))
        calls.append(self)


def run_spec(platform: str) -> dict[str, Any]:
    """Execute openberry.spec as PyInstaller would, for `platform`. Returns its namespace plus `calls`."""
    calls: list[Recorder] = []
    namespace: dict[str, Any] = {
        name: functools.partial(Recorder, name, calls) for name in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")
    }
    namespace.update(SPECPATH=str(PACKAGING), SPEC=str(PACKAGING / "openberry.spec"), TARGET_PLATFORM=platform,
                     __name__="__main__", calls=calls)
    code = compile((PACKAGING / "openberry.spec").read_text(encoding="utf-8"), "openberry.spec", "exec")
    exec(code, namespace)  # noqa: S102 (our own build recipe)
    return namespace


@pytest.fixture(scope="module")
def specs() -> dict[str, dict[str, Any]]:
    """The spec run for Linux, macOS and Windows. PyInstaller's collect_* helpers are cached: each takes seconds."""
    hooks = pytest.importorskip("PyInstaller.utils.hooks")
    originals = {name: getattr(hooks, name) for name in ("collect_data_files", "collect_submodules", "copy_metadata")}
    for name, real in originals.items():
        cached = functools.cache(lambda *args, _real=real, **kwargs: tuple(_real(*args, **kwargs)))
        setattr(hooks, name, lambda *args, _cached=cached, **kwargs: list(_cached(*args, **kwargs)))
    try:
        return {platform: run_spec(platform) for platform in ("linux", "darwin", "win32")}
    finally:
        for name, real in originals.items():
            setattr(hooks, name, real)


def calls_of(spec: dict[str, Any], kind: str) -> list[Recorder]:
    return [call for call in spec["calls"] if call.kind == kind]


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_spec_builds_a_windowed_app_and_a_console_cli_from_one_analysis(specs: dict[str, Any], platform: str) -> None:
    spec = specs[platform]
    (analysis,) = calls_of(spec, "Analysis")
    assert analysis.args[0] == [str(PACKAGING / "launcher.py")]
    app, cli = calls_of(spec, "EXE")
    assert (app.kwargs["name"], app.kwargs["console"]) == ("OpenBerry", False)
    assert (cli.kwargs["name"], cli.kwargs["console"]) == ("openberry-cli", True)
    for exe in (app, cli):
        assert exe.kwargs["exclude_binaries"] is True and exe.kwargs["upx"] is False
    (collect,) = calls_of(spec, "COLLECT")
    assert collect.args[:2] == (app, cli)  # the app first: macOS starts the bundle's first executable
    assert collect.kwargs["name"] == "OpenBerry"


def test_spec_names_the_cli_as_the_help_page_and_build_script_expect(specs: dict[str, Any]) -> None:
    assert specs["linux"]["CLI_NAME"] == pages.CLI_EXECUTABLE == build.CLI_NAME == "openberry-cli"
    assert specs["linux"]["APP_NAME"] == build.APP_NAME == "OpenBerry"
    assert specs["linux"]["VERSION"] == openberry.__version__


def test_spec_bundles_templates_static_files_and_needed_metadata(specs: dict[str, Any]) -> None:
    (analysis,) = calls_of(specs["linux"], "Analysis")
    destinations = {Path(dest).as_posix() for _, dest in analysis.kwargs["datas"]}
    assert {"openberry/web/templates", "openberry/web/static"} <= destinations
    sources = {Path(src).name for src, _ in analysis.kwargs["datas"]}
    assert {"base.html", "help.html", "app.css", "favicon.svg"} <= sources
    for distribution in ("openberry", "httpx2", "httpcore2"):
        assert any(re.fullmatch(rf"{distribution}-[\d.]+\.dist-info", d) for d in destinations), distribution


def test_spec_names_modules_imported_by_name(specs: dict[str, Any]) -> None:
    (analysis,) = calls_of(specs["linux"], "Analysis")
    hidden = set(analysis.kwargs["hiddenimports"])
    assert {"uvicorn.loops.auto", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on",
            "anyio._backends._asyncio", "openberry.collectors.news", "openberry.desktop", "openberry.mcp_server",
            "mcp_types", "feedparser"} <= hidden


def test_spec_leaves_pywebview_out_on_linux_only(specs: dict[str, Any]) -> None:
    linux = calls_of(specs["linux"], "Analysis")[0].kwargs
    assert {"webview", "bottle", "proxy_tools"} <= set(linux["excludes"])
    assert not any(name.startswith("webview") for name in linux["hiddenimports"])
    windows = calls_of(specs["win32"], "Analysis")[0].kwargs
    assert "webview" not in windows["excludes"]
    assert {"webview.platforms.winforms", "webview.platforms.edgechromium"} <= set(windows["hiddenimports"])
    macos = calls_of(specs["darwin"], "Analysis")[0].kwargs
    assert "webview" not in macos["excludes"] and "webview.platforms.cocoa" in macos["hiddenimports"]


@pytest.mark.parametrize(("platform", "icon"), [("win32", "openberry.ico"), ("darwin", "openberry.icns"),
                                                ("linux", None)])
def test_spec_uses_the_platforms_icon(specs: dict[str, Any], platform: str, icon: str | None) -> None:
    for exe in calls_of(specs[platform], "EXE"):
        if icon is None:
            assert exe.kwargs["icon"] is None
        else:
            assert Path(exe.kwargs["icon"]) == PACKAGING / "icons" / icon and Path(exe.kwargs["icon"]).is_file()


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_spec_leaves_out_gnu_readline_and_curses(specs: dict[str, Any], platform: str) -> None:
    """Python's readline module links GNU Readline (GPL-3.0) on Linux: it must not be in any download."""
    assert {"readline", "_curses", "curses"} <= set(calls_of(specs[platform], "Analysis")[0].kwargs["excludes"])


def test_spec_makes_a_mac_app_bundle_that_is_a_normal_app(specs: dict[str, Any]) -> None:
    (bundle,) = calls_of(specs["darwin"], "BUNDLE")
    (collect,) = calls_of(specs["darwin"], "COLLECT")
    assert bundle.args == (collect,)
    assert bundle.kwargs["name"] == "OpenBerry.app"
    assert bundle.kwargs["bundle_identifier"] == "io.openberry.desktop"
    assert Path(bundle.kwargs["icon"]).name == "openberry.icns"
    plist = bundle.kwargs["info_plist"]
    assert plist["NSHighResolutionCapable"] is True
    assert plist["LSBackgroundOnly"] is False  # else no Dock icon and no window: COLLECT's console is the CLI's
    assert plist["CFBundleShortVersionString"] == openberry.__version__
    assert not calls_of(specs["linux"], "BUNDLE") and not calls_of(specs["win32"], "BUNDLE")


# ======================================================================================
# build.py
# ======================================================================================

def test_versions_agree() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert build.read_version() == openberry.__version__ == pyproject["project"]["version"]


def test_read_version_explains_a_missing_version(tmp_path: Path) -> None:
    (tmp_path / "src" / "openberry").mkdir(parents=True)
    (tmp_path / "src" / "openberry" / "__init__.py").write_text('"""no version"""\n', encoding="utf-8")
    with pytest.raises(build.BuildError, match="no __version__"):
        build.read_version(tmp_path)


@pytest.mark.parametrize(("system", "machine", "tag"), [
    ("win32", "AMD64", "windows-x64"), ("darwin", "arm64", "macos-arm64"), ("darwin", "x86_64", "macos-x64"),
    ("linux", "x86_64", "linux-x64"), ("linux", "aarch64", "linux-arm64"), ("freebsd14", "amd64", "freebsd-x64"),
])
def test_platform_tag(system: str, machine: str, tag: str) -> None:
    assert build.platform_tag(system, machine) == tag


def test_archive_names() -> None:
    assert build.archive_name("windows-x64") == "OpenBerry-windows-x64.zip"
    assert build.archive_name("macos-arm64") == "OpenBerry-macos-arm64.zip"
    assert build.archive_name("linux-x64") == "OpenBerry-linux-x64.tar.gz"


def test_layouts(tmp_path: Path) -> None:
    mac = build.Layout.for_platform(tmp_path, "darwin")
    assert mac.app == tmp_path / "OpenBerry.app"
    assert mac.gui == tmp_path / "OpenBerry.app" / "Contents" / "MacOS" / "OpenBerry"
    assert mac.cli == tmp_path / "OpenBerry.app" / "Contents" / "MacOS" / "openberry-cli"
    windows = build.Layout.for_platform(tmp_path, "win32")
    assert (windows.app, windows.gui.name, windows.cli.name) == (tmp_path / "OpenBerry", "OpenBerry.exe",
                                                                  "openberry-cli.exe")
    linux = build.Layout.for_platform(tmp_path, "linux")
    assert (linux.gui, linux.cli) == (tmp_path / "OpenBerry" / "OpenBerry", tmp_path / "OpenBerry" / "openberry-cli")


def test_pyinstaller_command(tmp_path: Path) -> None:
    command = build.pyinstaller_command(dist=tmp_path / "d", work=tmp_path / "w", python="py")
    assert command[:3] == ["py", "-m", "PyInstaller"] and "--noconfirm" in command
    assert command[command.index("--distpath") + 1] == str(tmp_path / "d")
    assert command[-1] == str(PACKAGING / "openberry.spec")


def test_missing_pyinstaller_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "PyInstaller", None)  # makes `import PyInstaller` fail
    with pytest.raises(build.BuildError, match="pip install .*pyinstaller"):
        build.check_pyinstaller()


def test_failed_pyinstaller_run_stops_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "check_pyinstaller", lambda: None)
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(build.BuildError, match="PyInstaller failed"):
        build.run_pyinstaller()


def fake_app(folder: Path) -> Path:
    """A built app folder with an executable and a library."""
    script(folder / "OpenBerry", "echo hi")
    (folder / "_internal").mkdir()
    (folder / "_internal" / "lib.so").write_bytes(b"x" * 10)
    return folder


def test_build_puts_the_license_files_in_every_download(built: tuple[Path, Path]) -> None:
    dist, output = built
    assert build.main(["--skip-build", "--dist", str(dist)]) == 0
    archive = Path(github_outputs(output)["archive"])
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as tar:
            names = set(tar.getnames())
    else:
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
    top = "" if sys.platform == "darwin" else "OpenBerry/"  # next to OpenBerry.app on a Mac, else in the folder
    assert {top + build.LICENSE_FILE, top + build.NOTICES_FILE} <= names, names


@pytest.mark.parametrize("system", ["win32", "linux"])
def test_windows_and_linux_downloads_hold_the_license_files_in_the_app_folder(tmp_path: Path, system: str) -> None:
    app = fake_app(tmp_path / "dist" / "OpenBerry")
    build.write_license_files(app, "1.0", system)
    name = "OpenBerry-1.0-linux-x64.tar.gz" if system == "linux" else "OpenBerry-1.0-windows-x64.zip"
    archive = build.make_archive(app, tmp_path / "dist" / name, system)
    if system == "linux":
        with tarfile.open(archive) as tar:
            names = set(tar.getnames())
    else:
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
    assert {"OpenBerry/LICENSE.txt", "OpenBerry/THIRD_PARTY_NOTICES.txt"} <= names


def test_mac_download_holds_the_license_files_next_to_the_app(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "OpenBerry.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    files = build.write_license_files(tmp_path / "licenses", "1.0", "darwin")
    staged: list[set[str]] = []

    def ditto(command: list[str], **_: Any) -> None:
        staged.append({p.name for p in Path(command[3]).iterdir()})
        assert (Path(command[3]) / "OpenBerry.app" / "Contents" / "MacOS").is_dir()  # moved, with everything in it

    monkeypatch.setattr(build.subprocess, "run", ditto)
    archive = tmp_path / "OpenBerry-1.0-macos-arm64.zip"
    build.make_archive(app, archive, "darwin", beside=files)
    assert staged == [{"OpenBerry.app", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt"}]
    assert (app / "Contents" / "MacOS").is_dir()  # back where the smoke test looks for it
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []  # no staging folder left


def test_license_file_is_openberrys_mit_license(tmp_path: Path) -> None:
    license_file, notices = build.write_license_files(tmp_path, "1.0", "linux")
    assert license_file.read_text(encoding="utf-8") == (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_file.read_text(encoding="utf-8")
    assert notices.name == "THIRD_PARTY_NOTICES.txt"


def test_notices_list_what_the_app_bundles_with_license_texts() -> None:
    text = build.notices_text("1.0", "linux")
    packages = text.split("Python packages", 1)[1].split("\n\n=", 1)[0]
    listed = {line[2:].split(":")[0].rsplit(" ", 1)[0].lower() for line in packages.splitlines()
              if line.startswith("- ")}
    assert {"fastapi", "starlette", "uvicorn", "httpx", "mcp", "pydantic", "jinja2", "certifi", "feedparser"} <= listed
    assert not {"pytest", "pyinstaller", "playwright", "pillow"} & listed  # development tools are not bundled
    assert "pywebview" not in listed  # the Linux app has no window
    assert "- certifi " in text and "MPL-2.0" in text and "Mozilla Public License" in text  # its text, not only a name
    for component in ("Python 3.", "OpenSSL", "SQLite", "PyInstaller bootloader"):
        assert f"- {component}" in text, component
    assert "WebView2" not in text


def test_windows_notices_include_webview2_and_pywebview() -> None:
    pytest.importorskip("webview")
    text = build.notices_text("1.0", "win32")
    assert "Microsoft Edge WebView2 SDK" in text and "Copyright (C) Microsoft Corporation" in text
    assert "- pywebview " in text


def test_bundled_distributions_follow_requirements_and_extras() -> None:
    names = {build._normalize(d.metadata["Name"]) for d in build.bundled_distributions("openberry")}
    assert {"fastapi", "starlette", "uvicorn", "httptools", "anyio"} <= names  # uvicorn[standard] brings httptools
    assert "openberry" not in names and "pytest" not in names


def test_linux_archive_keeps_the_folder_and_the_executable_bit(tmp_path: Path) -> None:
    app = fake_app(tmp_path / "dist" / "OpenBerry")
    archive = build.make_archive(app, tmp_path / "dist" / "OpenBerry-1.0-linux-x64.tar.gz", "linux")
    with tarfile.open(archive) as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert {"OpenBerry", "OpenBerry/OpenBerry", "OpenBerry/_internal/lib.so"} <= set(members)
    if os.name != "nt":
        assert members["OpenBerry/OpenBerry"].mode & stat.S_IXUSR


def test_windows_archive_is_a_zip_of_the_folder(tmp_path: Path) -> None:
    app = fake_app(tmp_path / "dist" / "OpenBerry")
    old = tmp_path / "dist" / "OpenBerry-1.0-windows-x64.zip"
    old.write_bytes(b"an older build")
    archive = build.make_archive(app, old, "win32")
    with zipfile.ZipFile(archive) as zf:
        assert {"OpenBerry/OpenBerry", "OpenBerry/_internal/lib.so"} <= set(zf.namelist())


def test_mac_archive_without_extra_files_is_made_with_ditto(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "OpenBerry.app"
    app.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(build.subprocess, "run", lambda command, **_: commands.append(command))
    archive = tmp_path / "OpenBerry-1.0-macos-arm64.zip"
    build.make_archive(app, archive, "darwin")
    assert commands == [["ditto", "-c", "-k", "--keepParent", str(app), str(archive)]]


def test_archiving_a_missing_app_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(build.BuildError, match="build the app first"):
        build.make_archive(tmp_path / "OpenBerry", tmp_path / "x.zip", "win32")


@pytest.mark.parametrize("tag", ["", "v" + openberry.__version__, openberry.__version__])
def test_release_tag_matching_the_version_passes(tag: str) -> None:
    build.check_release_tag(tag, openberry.__version__)


def test_release_tag_must_match_the_version() -> None:
    with pytest.raises(build.BuildError, match=r"tag v9\.9\.9 does not match .* 0\.1\.0.*set __version__"):
        build.check_release_tag("v9.9.9", "0.1.0")


def test_github_output(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    out.write_text("earlier=1\n", encoding="utf-8")
    assert build.write_github_output({"cli": "a/b", "gui": "c"}, {"GITHUB_OUTPUT": str(out)}) is True
    assert out.read_text(encoding="utf-8") == "earlier=1\ncli=a/b\ngui=c\n"
    assert build.write_github_output({"cli": "a"}, {}) is False


def test_folder_size(tmp_path: Path) -> None:
    assert build.folder_size(fake_app(tmp_path / "app")) == (tmp_path / "app" / "OpenBerry").stat().st_size + 10


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A dist folder holding a (fake) build for this machine, and the GITHUB_OUTPUT file."""
    layout = build.Layout.for_platform(tmp_path / "dist")
    for executable in (layout.gui, layout.cli):
        script(executable, "true")
    output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    return tmp_path / "dist", output


def github_outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def test_build_archives_the_app_and_reports_its_paths(built: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
                                                      ) -> None:
    dist, output = built
    assert build.main(["--skip-build", "--dist", str(dist)]) == 0
    outputs = github_outputs(output)
    layout = build.Layout.for_platform(dist)
    assert outputs["cli"] == layout.cli.as_posix() and outputs["gui"] == layout.gui.as_posix()
    assert outputs["app"] == layout.app.as_posix() and outputs["version"] == openberry.__version__
    archive = Path(outputs["archive"])
    assert archive.name == build.archive_name(build.platform_tag()) and archive.is_file()
    assert "Download:" in capsys.readouterr().out


def test_build_without_executables_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert build.main(["--skip-build", "--dist", str(tmp_path)]) == 1
    assert "build failed: the build has no" in capsys.readouterr().err


def test_release_build_with_the_wrong_tag_stops_before_pyinstaller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(build, "run_pyinstaller", lambda **_: pytest.fail("PyInstaller must not run"))
    assert build.main(["--dist", str(tmp_path), "--release-tag", "v99.0.0"]) == 1
    assert "does not match" in capsys.readouterr().err


# ======================================================================================
# smoke_test.py
# ======================================================================================

def test_clean_env_isolates_the_app(tmp_path: Path) -> None:
    env = smoke.clean_env(tmp_path, {"PATH": "/bin", "OPENBERRY_DB": "/mine.db", "OPENBERRY_PASSWORD": "x"})
    assert env["PATH"] == "/bin" and "OPENBERRY_DB" not in env and "OPENBERRY_PASSWORD" not in env
    assert env["OPENBERRY_HOME"] == str(tmp_path) and env["OPENBERRY_SCHEDULER"] == "false"
    assert not Path(env["OPENBERRY_ENV_FILE"]).exists()


def test_free_port_is_bindable() -> None:
    import socket

    port = smoke.free_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def reader_of(*lines: str) -> Any:
    return smoke.LineReader(io.StringIO("".join(lines)))


def test_jsonrpc_response_skips_notifications_and_returns_the_answer() -> None:
    reader = reader_of('{"jsonrpc": "2.0", "method": "notifications/message"}\n', '{"jsonrpc": "2.0", "id": 1, '
                       '"result": {"ok": true}}\n')
    assert smoke.jsonrpc_response(reader, 1, timeout=5)["result"] == {"ok": True}


@pytest.mark.parametrize(("lines", "message"), [
    (["INFO starting\n"], "not JSON-RPC"),
    (['{"id": 1, "result": {}}\n'], "not JSON-RPC"),
    (['{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}}\n'], "with an error"),
    ([], "closed its output"),
])
def test_jsonrpc_response_failures(lines: list[str], message: str) -> None:
    with pytest.raises(smoke.SmokeFailure, match=message):
        smoke.jsonrpc_response(reader_of(*lines), 1, timeout=5)


def test_line_reader_times_out() -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, encoding="utf-8")
    reader = smoke.LineReader(stream)
    try:
        with pytest.raises(smoke.SmokeFailure, match="no answer within"):
            reader.next_line(timeout=0.1)
    finally:
        os.close(write_fd)  # the reader thread sees the end of the output and stops
        reader.thread.join(5)
        stream.close()
    assert reader.next_line(timeout=1) is None


def test_wait_until_healthy_notices_a_server_that_exited() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    proc.wait()
    with pytest.raises(smoke.SmokeFailure, match="exited with code 3"):
        smoke.wait_until_healthy(f"http://127.0.0.1:{smoke.free_port()}", proc, timeout=5)


def test_only_openberrys_health_check_counts() -> None:
    """Another program answering on the port must not pass for the app (the same rule as desktop.py)."""
    from openberry import desktop

    for data in ({"ok": True, "app": "openberry"}, {"ok": True}, {"ok": "yes", "app": "openberry"},
                 {"status": "ok"}, [], None):
        assert smoke.is_openberry_health(data) is desktop.is_openberry_health(data), data
    assert smoke.is_openberry_health({"ok": True, "app": "openberry"})
    assert not smoke.is_openberry_health({"ok": True})


def test_executable_argument(tmp_path: Path) -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError, match="not a file"):
        smoke.executable(str(tmp_path / "missing"))
    if os.name != "nt":
        (tmp_path / "plain").write_text("", encoding="utf-8")
        with pytest.raises(argparse.ArgumentTypeError, match="not executable"):
            smoke.executable(str(tmp_path / "plain"))


@posix_only
def test_smoke_test_passes_against_the_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The whole smoke test, against the source app run as the packaged executables run it."""
    app = source_app(tmp_path / "bin" / "openberry")  # the name /help shows for a source install
    assert smoke.main([str(app), str(app)]) == 0
    out = capsys.readouterr().out
    for check in ("version: openberry " + openberry.__version__,
                  "serve: registered a company and a lead, served /c/1, /c/1/leads/1, /help",
                  "mcp: protocol 2025-06-18, 25 tools, list_companies sees 1 company", "app --smoke: --smoke passed"):
        assert check in out
    assert "Smoke test passed" in out


@posix_only
def test_smoke_test_stops_at_the_first_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = script(tmp_path / "openberry-cli", "echo 'missing module mcp_types' >&2; exit 3")
    assert smoke.main([str(broken)]) == 1
    err = capsys.readouterr().err
    assert "FAIL version: --version exited with code 3" in err and "missing module mcp_types" in err
    assert "Smoke test FAILED" in err


@posix_only
def test_mcp_check_rejects_anything_but_json_rpc_on_stdout(tmp_path: Path) -> None:
    noisy = script(tmp_path / "openberry-cli", 'echo "Starting OpenBerry MCP..."; cat > /dev/null')
    with pytest.raises(smoke.SmokeFailure, match="not JSON-RPC"):
        smoke.check_mcp(noisy, smoke.clean_env(tmp_path / "home"), tmp_path)


@posix_only
def test_app_check_reads_the_log_of_an_app_without_a_console(tmp_path: Path) -> None:
    """A windowed Windows app has no stdout: its log file is the evidence."""
    home = tmp_path / "home"
    quiet = script(tmp_path / "OpenBerry", 'mkdir -p "$OPENBERRY_HOME/logs" && '
                                           'echo "INFO smoke test passed at http://127.0.0.1:8000" '
                                           '> "$OPENBERRY_HOME/logs/desktop.log"')
    assert smoke.check_app(quiet, smoke.clean_env(home), tmp_path, home) == "--smoke passed"


@posix_only
def test_app_check_fails_without_proof_and_shows_the_log(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "desktop.log").write_text("ERROR could not start: no templates\n", encoding="utf-8")
    silent = script(tmp_path / "OpenBerry", "exit 0")
    with pytest.raises(smoke.SmokeFailure, match="(?s)without saying 'smoke test passed'.*no templates"):
        smoke.check_app(silent, smoke.clean_env(home), tmp_path, home)


def test_checks_include_the_app_only_when_given(tmp_path: Path) -> None:
    names = [name for name, _ in smoke.checks_for(tmp_path / "cli", None, tmp_path / "h", tmp_path)]
    assert names == ["version", "serve", "mcp"]
    names = [name for name, _ in smoke.checks_for(tmp_path / "cli", tmp_path / "app", tmp_path / "h", tmp_path)]
    assert names[-1] == "app --smoke"


# ======================================================================================
# The GitHub Actions workflow
# ======================================================================================

@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_triggers(workflow: dict[Any, Any]) -> None:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true
    watched = {"src/**", "tests/**", "packaging/**", "pyproject.toml", "uv.lock", ".github/workflows/desktop.yml"}
    assert set(on["pull_request"]["paths"]) == watched
    assert on["push"]["branches"] == ["main"] and set(on["push"]["paths"]) == watched
    assert on["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in on
    assert workflow["permissions"] == {"contents": "read"}


def step_using(steps: list[dict[str, Any]], action: str) -> dict[str, Any]:
    return next(step for step in steps if step.get("uses", "").startswith(action + "@"))


def step_running(steps: list[dict[str, Any]], text: str) -> dict[str, Any]:
    return next(step for step in steps if text in step.get("run", ""))


def test_workflow_builds_and_smoke_tests_on_every_platform(workflow: dict[Any, Any]) -> None:
    job = workflow["jobs"]["build"]
    matrix = {entry["os"]: entry for entry in job["strategy"]["matrix"]["include"]}
    assert set(matrix) == {"windows-latest", "macos-latest", "ubuntu-latest"}
    assert "desktop" in matrix["windows-latest"]["extras"] and "desktop" in matrix["macos-latest"]["extras"]
    assert "desktop" not in matrix["ubuntu-latest"]["extras"]  # no pywebview in the Linux app
    steps = job["steps"]
    step_using(steps, "actions/checkout")
    uv = step_using(steps, "astral-sh/setup-uv")
    assert uv["with"]["python-version"] == "3.12"
    install = step_running(steps, "uv sync")["run"]
    assert "--locked" in install and "${{ matrix.extras }}" in install and "--extra dev" in install
    assert "--group build" in install and "uv pip install" not in install  # PyInstaller comes from uv.lock
    tests = step_running(steps, "pytest")
    assert "if" not in tests  # on Windows, macOS and Linux
    lint = step_running(steps, "ruff")
    assert re.fullmatch(r"uvx ruff@[\d.]+ check src tests packaging", lint["run"]) and lint["if"] == "runner.os == 'Linux'"
    chromium = step_running(steps, "playwright install")
    assert "--with-deps chromium" in chromium["run"] and chromium["if"] == "runner.os == 'Linux'"
    assert steps.index(chromium) < steps.index(tests)
    build_step = step_running(steps, "packaging/build.py")
    assert build_step["id"] == "build" and '--release-tag "$RELEASE_TAG"' in build_step["run"]
    smoke_step = step_running(steps, "packaging/smoke_test.py")
    assert smoke_step["run"].endswith('packaging/smoke_test.py "$CLI" "$GUI"')
    assert smoke_step["env"] == {"CLI": "${{ steps.build.outputs.cli }}", "GUI": "${{ steps.build.outputs.gui }}"}
    upload = step_using(steps, "actions/upload-artifact")
    assert upload["with"]["path"] == "${{ steps.build.outputs.archive }}" and upload["with"]["retention-days"] == 14
    assert upload["with"]["name"].startswith("OpenBerry-")


# The first major version of each action that runs on Node 24: GitHub no longer runs Node 20 actions.
NODE24_ACTIONS = {"actions/checkout": 5, "astral-sh/setup-uv": 7, "actions/upload-artifact": 6,
                  "actions/download-artifact": 7}
WORKFLOWS = (WORKFLOW, ROOT / ".github" / "workflows" / "docker.yml")
PINNED = re.compile(r"^\s*(?:- )?uses: ([\w.-]+/[\w.-]+)@([0-9a-f]{40}) # v(\d+)\.\d+\.\d+$")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_actions_are_pinned_to_commits_and_run_on_node_24(path: Path) -> None:
    """A tag can be moved to other code; a commit can't. The comment says which version it is, for Dependabot."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "uses:" in line]
    assert lines
    for line in lines:
        match = PINNED.match(line)
        assert match, f"not pinned to a commit with a version comment: {line.strip()}"
        action, _, major = match.groups()
        assert action in NODE24_ACTIONS and int(major) >= NODE24_ACTIONS[action], line.strip()


def test_the_same_action_is_pinned_to_the_same_commit_everywhere() -> None:
    pins: dict[str, set[str]] = {}
    for path in WORKFLOWS:
        for match in filter(None, map(PINNED.match, path.read_text(encoding="utf-8").splitlines())):
            pins.setdefault(match[1], set()).add(match[2])
    assert pins and all(len(commits) == 1 for commits in pins.values()), pins


def test_dependabot_updates_the_actions_and_the_base_images() -> None:
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert {u["package-ecosystem"] for u in config["updates"]} == {"github-actions", "docker"}


def test_workflow_uses_only_outputs_the_build_script_writes(built: tuple[Path, Path]) -> None:
    dist, output = built
    assert build.main(["--skip-build", "--dist", str(dist)]) == 0
    used = set(re.findall(r"steps\.build\.outputs\.(\w+)", WORKFLOW.read_text(encoding="utf-8")))
    assert used and used <= set(github_outputs(output))


DOWNLOADS = ("OpenBerry-windows-x64.zip", "OpenBerry-macos-arm64.zip", "OpenBerry-linux-x64.tar.gz")


def test_release_downloads_are_the_three_the_build_makes() -> None:
    assert set(DOWNLOADS) == {build.archive_name(t) for t in ("windows-x64", "macos-arm64", "linux-x64")}


def test_workflow_publishes_tagged_releases_as_a_draft_first(workflow: dict[Any, Any]) -> None:
    release = workflow["jobs"]["release"]
    assert release["needs"] == "build" and release["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert release["permissions"] == {"contents": "write"}
    assert release["env"]["TAG"] == "${{ github.ref_name }}" and release["env"]["GH_TOKEN"] == "${{ github.token }}"
    steps = release["steps"]
    download = step_using(steps, "actions/download-artifact")
    assert download["with"]["pattern"] == "OpenBerry-*" and download["with"]["merge-multiple"] is True
    checksums = step_running(steps, "sha256sum")
    assert checksums["working-directory"] == download["with"]["path"] == "downloads"
    assert "sha256sum OpenBerry-* > SHA256SUMS" in checksums["run"]
    assert all(name in checksums["run"] for name in DOWNLOADS)  # all three, or nothing is published
    draft = step_running(steps, "gh release create")
    create = next(line for line in draft["run"].splitlines() if "gh release create" in line)
    assert '"$TAG" downloads/*' in create and "--draft" in create and '--title "OpenBerry $TAG"' in create
    publish = step_running(steps, "gh release edit")
    assert "--draft=false" in publish["run"] and "--latest" in publish["run"]
    assert steps.index(download) < steps.index(checksums) < steps.index(draft) < steps.index(publish)


def test_workflow_checks_the_latest_download_links_after_publishing(workflow: dict[Any, Any]) -> None:
    check = workflow["jobs"]["check-downloads"]
    assert check["needs"] == "release" and "permissions" not in check
    (step,) = check["steps"]
    assert step["env"]["LATEST"] == "${{ github.server_url }}/${{ github.repository }}/releases/latest/download"
    assert all(name in step["run"] for name in DOWNLOADS)
    assert '[ "$code" = 200 ]' in step["run"] and "exit $failed" in step["run"]


# ======================================================================================
# Docker
# ======================================================================================

def test_docker_image_installs_exactly_the_locked_packages() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert "uv export --frozen --no-dev --no-emit-project" in dockerfile
    assert "pip install --require-hashes -r /tmp/requirements.txt" in dockerfile
    assert "pip install --no-deps ." in dockerfile and "pip install ." not in dockerfile
    froms = re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE)
    assert len(froms) == 3
    for image in froms:
        assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image), image
    assert any(re.match(r"python:3\.12\.\d+-slim", image) for image in froms)
    assert 'CMD ["openberry", "serve", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile  # cli._serve's checks
    assert "chmod 700 /data" in dockerfile and "USER openberry" in dockerfile


def test_compose_runs_the_app_hardened() -> None:
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    app = compose["services"]["openberry"]
    assert app["stop_grace_period"] == "45s" and app["read_only"] is True and app["tmpfs"] == ["/tmp"]
    assert app["security_opt"] == ["no-new-privileges:true"] and app["cap_drop"] == ["ALL"]
    assert app["ports"] == ["127.0.0.1:8000:8000"]
    ollama = compose["services"]["ollama"]["image"]
    assert re.fullmatch(r"ollama/ollama:\d+\.\d+\.\d+", ollama)


def test_docker_build_context_leaves_out_tests_scripts_and_docs() -> None:
    ignored = set((ROOT / ".dockerignore").read_text(encoding="utf-8").split())
    assert {"tests", "scripts", "docs", ".env", "*.db"} <= ignored


def test_docker_workflow_builds_checks_and_runs_the_image() -> None:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8"))
    on = workflow.get("on", workflow.get(True))
    watched = {"Dockerfile", "docker-compose.yml", "src/**", "uv.lock", "pyproject.toml"}
    assert watched <= set(on["pull_request"]["paths"]) and watched <= set(on["push"]["paths"])
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["image"]["steps"]
    order = [step_running(steps, text) for text in (
        "docker compose config", "docker compose build", "packaging/docker_check.py openberry:latest",
        "OpenBerry has no password", "OPENBERRY_PASSWORD=", "State.Health.Status")]
    assert [steps.index(step) for step in order] == sorted(steps.index(step) for step in order)
    assert "docker compose up -d --no-build" in step_running(steps, "OPENBERRY_PASSWORD=")["run"]
    assert step_running(steps, "docker compose down")["if"] == "always()"


def test_docker_check_finds_every_difference() -> None:
    wanted = {"fastapi": "0.143.0", "uvloop": "0.23.0", "Jinja2": "3.1.6"}
    installed = {"fastapi": "0.143.0", "jinja2": "3.1.6", "uvloop": "0.23.0", "pip": "25.0", "openberry": "1.0"}
    assert docker_check.differences(wanted, installed) == []
    installed = {"fastapi": "0.144.0", "jinja2": "3.1.6", "rich": "14.0", "pip": "25.0"}
    assert docker_check.differences(wanted, installed) == [
        "fastapi: locked 0.143.0, installed 0.144.0", "uvloop 0.23.0 is locked but not installed",
        "rich 14.0 is installed but not in uv.lock"]


# ======================================================================================
# The project's tooling
# ======================================================================================

def test_pyproject_locks_pyinstaller_and_the_test_tools() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_group = " ".join(pyproject["dependency-groups"]["build"])
    assert "pyinstaller>=" in build_group and "pyinstaller-hooks-contrib" in build_group
    dev = " ".join(pyproject["project"]["optional-dependencies"]["dev"])
    assert "playwright" in dev and "pillow" in dev
    assert pyproject["tool"]["ruff"]["lint"]["select"] == ["F", "E9", "B"]
    assert {"error::DeprecationWarning", "error::PendingDeprecationWarning"} <= set(
        pyproject["tool"]["pytest"]["ini_options"]["filterwarnings"])
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    for name in ("pyinstaller", "pyinstaller-hooks-contrib", "playwright", "pillow"):
        assert f'name = "{name}"' in lock, name


def test_collected_test_ids_stay_short(request: pytest.FixtureRequest) -> None:
    """A parametrized test whose id is its whole input (200,000 spaces) floods every report."""
    long = [item.nodeid[:80] for item in request.session.items if len(item.nodeid) > 300]
    assert long == []


# ======================================================================================
# Docs
# ======================================================================================

def test_readme_links_the_desktop_guide() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Desktop app" in readme and "(docs/DESKTOP.md)" in readme


def test_desktop_guide_matches_the_build() -> None:
    guide = (ROOT / "docs" / "DESKTOP.md").read_text(encoding="utf-8")
    for tag in ("windows-x64", "macos-arm64", "linux-x64"):
        assert build.archive_name(tag) in guide
    assert "OpenBerry.app/Contents/MacOS/openberry-cli" in guide and "openberry-cli.exe" in guide
    for name in ("packaging/build.py", "packaging/smoke_test.py", "More info", "Run anyway", "Open Anyway",
                 "Connect Claude", "~/.openberry", "logs/desktop.log", "git tag v"):
        assert name in guide, name
    # Building from the lock, the license files, and releasing by pushing a tag only, with recovery steps.
    for name in ("--group build", build.LICENSE_FILE, build.NOTICES_FILE, "SHA256SUMS",
                 "Never create the release in GitHub's web page", "If a release goes wrong",
                 "Set as the latest release"):
        assert name in guide, name
    assert "uv pip install pyinstaller" not in guide
