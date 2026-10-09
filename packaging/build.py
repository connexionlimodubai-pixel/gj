"""Build the OpenBerry desktop app and the file people download: `python packaging/build.py`.

Runs PyInstaller with packaging/openberry.spec, then packs the result into one archive in dist/:

  OpenBerry-<version>-windows-x64.zip    the OpenBerry folder (OpenBerry.exe, openberry-cli.exe, _internal)
  OpenBerry-<version>-macos-arm64.zip    OpenBerry.app, zipped with `ditto` to keep its symlinks and signature
  OpenBerry-<version>-linux-x64.tar.gz   the OpenBerry folder, with its file permissions

Needs the project and PyInstaller in the Python that runs it, e.g.
`uv pip install -e '.[desktop]' pyinstaller pyinstaller-hooks-contrib` (no `[desktop]` on Linux).
In GitHub Actions it also writes the archive and executable paths to $GITHUB_OUTPUT.
See docs/DESKTOP.md.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "openberry.spec"
DIST = ROOT / "dist"
WORK = ROOT / "build"
APP_NAME = "OpenBerry"
CLI_NAME = "openberry-cli"
OS_NAMES = {"win32": "windows", "darwin": "macos", "linux": "linux"}
ARCH_NAMES = {"x86_64": "x64", "amd64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64"}


class BuildError(RuntimeError):
    """The build can't go on; the message says why."""


def read_version(root: Path = ROOT) -> str:
    """OpenBerry's version, from src/openberry/__init__.py (without importing it)."""
    text = (root / "src" / "openberry" / "__init__.py").read_text(encoding="utf-8")
    if match := re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE):
        return match.group(1)
    raise BuildError("no __version__ in src/openberry/__init__.py")


def os_name(system: str = sys.platform) -> str:
    """windows, macos or linux (another Unix keeps its own name, e.g. freebsd14 -> freebsd)."""
    return OS_NAMES.get(system) or re.sub(r"\d+$", "", system)


def platform_tag(system: str = sys.platform, machine: str | None = None) -> str:
    """The part of the archive name that says where it runs: windows-x64, macos-arm64, linux-x64..."""
    machine = (platform.machine() if machine is None else machine).lower()
    return f"{os_name(system)}-{ARCH_NAMES.get(machine, machine or 'unknown')}"


def archive_name(version: str, tag: str) -> str:
    extension = "tar.gz" if tag.startswith("linux") else "zip"
    return f"{APP_NAME}-{version}-{tag}.{extension}"


@dataclass(frozen=True)
class Layout:
    """Where PyInstaller puts the app in `dist`, for one platform."""

    app: Path  # what goes into the archive: the OpenBerry folder, or OpenBerry.app on macOS
    gui: Path  # the app's executable (no console)
    cli: Path  # the console executable: `openberry-cli mcp` for Claude Desktop

    @classmethod
    def for_platform(cls, dist: Path = DIST, system: str = sys.platform) -> Layout:
        if system == "darwin":
            app = dist / f"{APP_NAME}.app"
            return cls(app, app / "Contents" / "MacOS" / APP_NAME, app / "Contents" / "MacOS" / CLI_NAME)
        exe = ".exe" if system == "win32" else ""
        app = dist / APP_NAME
        return cls(app, app / f"{APP_NAME}{exe}", app / f"{CLI_NAME}{exe}")


def pyinstaller_command(spec: Path = SPEC, dist: Path = DIST, work: Path = WORK,
                        python: str = sys.executable) -> list[str]:
    return [python, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
            "--distpath", str(dist), "--workpath", str(work), str(spec)]


def check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise BuildError(f"PyInstaller is not installed in {sys.executable}. Install it with: "
                         f"uv pip install --python {sys.executable} pyinstaller pyinstaller-hooks-contrib") from None


def run_pyinstaller(spec: Path = SPEC, dist: Path = DIST, work: Path = WORK) -> None:
    check_pyinstaller()
    command = pyinstaller_command(spec, dist, work)
    print("$", " ".join(command), flush=True)
    if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
        raise BuildError("PyInstaller failed (see above)")


def folder_size(path: Path) -> int:
    """Bytes in the files under `path` (symlinks count as links, not as what they point to)."""
    if path.is_file():
        return path.stat().st_size
    return sum(p.lstat().st_size for p in path.rglob("*") if p.is_symlink() or p.is_file())


def make_archive(app: Path, destination: Path, system: str = sys.platform) -> Path:
    """Pack the built app (folder or .app) into `destination`, keeping its top-level folder name."""
    if not app.exists():
        raise BuildError(f"{app} does not exist: build the app first")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    if system == "darwin":
        # zipfile and shutil would turn the bundle's symlinks into copies and break its code signature.
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(destination)], check=True)
    elif destination.name.endswith(".tar.gz"):
        with tarfile.open(destination, "w:gz") as tar:  # keeps the executables executable
            tar.add(app, arcname=app.name)
    else:
        base = destination.with_suffix("")  # make_archive adds .zip
        shutil.make_archive(str(base), "zip", root_dir=app.parent, base_dir=app.name)
    return destination


def write_github_output(values: Mapping[str, str], environ: Mapping[str, str] | None = None) -> bool:
    """Append name=value lines to $GITHUB_OUTPUT (GitHub Actions step outputs). False outside Actions."""
    path = (os.environ if environ is None else environ).get("GITHUB_OUTPUT")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as fh:
        for name, value in values.items():
            fh.write(f"{name}={value}\n")
    return True


def megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python packaging/build.py", description="Build the OpenBerry desktop app.")
    parser.add_argument("--skip-build", action="store_true", help="only archive the app already in dist/")
    parser.add_argument("--no-archive", action="store_true", help="only build the app, don't archive it")
    parser.add_argument("--dist", type=Path, default=DIST, help="output folder (default: dist/)")
    parser.add_argument("--release-tag", default="", metavar="vX.Y.Z",
                        help="fail unless OpenBerry's version is this tag's (for release builds)")
    return parser.parse_args(argv)


def check_release_tag(tag: str, version: str) -> None:
    """A release's downloads are named after OpenBerry's version: it must be the tag's."""
    if tag and tag.removeprefix("v") != version:
        wanted = tag.removeprefix("v")
        raise BuildError(f"the tag {tag} does not match OpenBerry's version {version}: "
                         f"set __version__ in src/openberry/__init__.py (and pyproject.toml) to {wanted}")


def build(args: argparse.Namespace) -> Layout:
    dist: Path = args.dist.resolve()
    version = read_version()
    check_release_tag(args.release_tag, version)
    layout = Layout.for_platform(dist)
    if not args.skip_build:
        run_pyinstaller(dist=dist)
    for executable in (layout.gui, layout.cli):
        if not executable.is_file():
            raise BuildError(f"the build has no {executable}")
    print(f"App: {layout.app} ({megabytes(folder_size(layout.app))})")
    # Forward slashes: the paths work in every shell, Git Bash on Windows included.
    outputs = {"version": version, "app": layout.app.as_posix(), "gui": layout.gui.as_posix(),
               "cli": layout.cli.as_posix()}
    if not args.no_archive:
        archive = make_archive(layout.app, dist / archive_name(version, platform_tag()))
        print(f"Download: {archive} ({megabytes(archive.stat().st_size)})")
        outputs["archive"] = archive.as_posix()
    write_github_output(outputs)
    return layout


def main(argv: Sequence[str] | None = None) -> int:
    try:
        build(parse_args(argv))
    except (BuildError, subprocess.CalledProcessError, OSError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
