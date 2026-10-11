"""Build the OpenBerry desktop app and the file people download: `python packaging/build.py`.

Runs PyInstaller with packaging/openberry.spec, then packs the result into one archive in dist/:

  OpenBerry-windows-x64.zip    the OpenBerry folder (OpenBerry.exe, openberry-cli.exe, _internal)
  OpenBerry-macos-arm64.zip    OpenBerry.app, zipped with `ditto` to keep its symlinks and signature
  OpenBerry-linux-x64.tar.gz   the OpenBerry folder, with its file permissions

Each download also holds LICENSE.txt (OpenBerry's MIT license) and THIRD_PARTY_NOTICES.txt (the licenses of
everything the app bundles): in the OpenBerry folder, and next to OpenBerry.app on a Mac, where adding files
inside the signed app would break its signature.

The names carry no version number on purpose: the release says which version it is, and
https://github.com/<owner>/<repo>/releases/latest/download/<name> then always gets the newest one.

Needs the project and PyInstaller in the Python that runs it, e.g.
`uv pip install -e '.[desktop]' pyinstaller pyinstaller-hooks-contrib` (no `[desktop]` on Linux).
In GitHub Actions it also writes the archive and executable paths to $GITHUB_OUTPUT.
See docs/DESKTOP.md.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import sysconfig
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "openberry.spec"
DIST = ROOT / "dist"
WORK = ROOT / "build"
APP_NAME = "OpenBerry"
CLI_NAME = "openberry-cli"
OS_NAMES = {"win32": "windows", "darwin": "macos", "linux": "linux"}
ARCH_NAMES = {"x86_64": "x64", "amd64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64"}
LICENSE_FILE = "LICENSE.txt"
NOTICES_FILE = "THIRD_PARTY_NOTICES.txt"


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


def archive_name(tag: str) -> str:
    """The download's file name: the same for every version, so .../releases/latest/download/<name> works."""
    extension = "tar.gz" if tag.startswith("linux") else "zip"
    return f"{APP_NAME}-{tag}.{extension}"


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


def make_archive(app: Path, destination: Path, system: str = sys.platform, beside: Sequence[Path] = ()) -> Path:
    """Pack the built app (folder or .app) into `destination`, keeping its top-level folder name.

    `beside`: files that go next to the app at the top of the archive (the license files of a Mac download).
    """
    if not app.exists():
        raise BuildError(f"{app} does not exist: build the app first")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    if system == "darwin":
        # zipfile and shutil would turn the bundle's symlinks into copies and break its code signature.
        if not beside:
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(destination)], check=True)
            return destination
        # ditto archives the contents of a folder: one holding the app (moved, never copied) and the files.
        stage = app.parent / f".{destination.name}.stage"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        try:
            for path in beside:
                shutil.copy2(path, stage / path.name)
            app.rename(stage / app.name)
            try:
                subprocess.run(["ditto", "-c", "-k", str(stage), str(destination)], check=True)
            finally:
                (stage / app.name).rename(app)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    elif destination.name.endswith(".tar.gz"):
        with tarfile.open(destination, "w:gz") as tar:  # keeps the executables executable
            tar.add(app, arcname=app.name)
    else:
        base = destination.with_suffix("")  # make_archive adds .zip
        shutil.make_archive(str(base), "zip", root_dir=app.parent, base_dir=app.name)
    return destination


# --------------------------------------------------------------------------------------
# License notices: OpenBerry's license and the licenses of what the app bundles
# --------------------------------------------------------------------------------------

_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")
_EXTRA_MARKER = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")
_LICENSE_FILE_NAME = re.compile(r"LICEN[CS]E|COPYING|NOTICE|AUTHORS", re.IGNORECASE)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _marker_applies(marker: str, extras: set[str]) -> bool:
    """Whether a requirement's environment marker holds here, for one of `extras` (or none)."""
    marker = marker.strip()
    if not marker:
        return True
    try:
        from packaging.markers import Marker  # PyInstaller depends on it: always there when building
    except ImportError:  # without it: only the extras are checked; an installed distribution counts as bundled
        wanted = {_normalize(e) for e in _EXTRA_MARKER.findall(marker)}
        return not wanted or bool(wanted & extras)
    parsed = Marker(marker)
    return any(parsed.evaluate({"extra": extra}) for extra in (sorted(extras) or [""]))


def bundled_distributions(root: str = "openberry", extras: Sequence[str] = ()) -> list[importlib.metadata.Distribution]:
    """The installed distributions the app bundles: what `root` (with `extras`) requires, and what those require.
    Development tools (the dev extra, PyInstaller) are not among them."""
    found: dict[str, tuple[importlib.metadata.Distribution, set[str]]] = {}
    todo: list[tuple[str, set[str]]] = [(root, {_normalize(e) for e in extras})]
    while todo:
        name, wanted = todo.pop()
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue  # not installed here, so not bundled either
        key = _normalize(dist.metadata["Name"] or name)
        if key in found and wanted <= found[key][1]:
            continue
        found[key] = (dist, (found[key][1] if key in found else set()) | wanted)
        for requirement in dist.requires or []:
            spec, _, marker = requirement.partition(";")
            match = _REQUIREMENT.match(spec)
            if match and _marker_applies(marker, wanted):
                todo.append((match[1], {_normalize(e) for e in (match[2] or "").split(",") if e.strip()}))
    return sorted((dist for key, (dist, _) in found.items() if key != _normalize(root)),
                  key=lambda d: _normalize(d.metadata["Name"] or ""))


def license_name(dist: importlib.metadata.Distribution) -> str:
    meta = dist.metadata
    if expression := (meta.get("License-Expression") or "").strip():
        return expression
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 80:
        return text
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return ", ".join(classifiers) or ("see the license text below" if text else "unknown")


def license_texts(dist: importlib.metadata.Distribution) -> Iterator[tuple[str, str]]:
    """(file name, text) of each license file in the distribution's metadata folder (.dist-info, and its licenses/
    folder)."""
    for file in dist.files or []:
        parts = PurePosixPath(file.as_posix()).parts
        if len(parts) < 2 or not parts[0].endswith(".dist-info") or not _LICENSE_FILE_NAME.search(parts[-1]):
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if text.strip():
            yield "/".join(parts[1:]), text.strip()


def _python_license() -> str:
    for folder in (Path(sys.base_prefix), Path(sysconfig.get_paths()["stdlib"])):
        for name in ("LICENSE.txt", "LICENSE"):
            try:
                return (folder / name).read_text(encoding="utf-8").strip()
            except OSError:
                continue
    return "PSF License Agreement: https://docs.python.org/3/license.html"


# The license of the Microsoft Edge WebView2 SDK, whose DLLs pywebview bundles for the Windows window.
WEBVIEW2_LICENSE = """Copyright (C) Microsoft Corporation. All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
following conditions are met:

   * Redistributions of source code must retain the above copyright notice, this list of conditions and the following
     disclaimer.
   * Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the
     following disclaimer in the documentation and/or other materials provided with the distribution.
   * The name of Microsoft Corporation, or the names of its contributors may not be used to endorse or promote
     products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE."""


def component_notices(system: str = sys.platform) -> list[tuple[str, str, str]]:
    """(name, license, text) of what the app bundles besides Python packages."""
    notices = [
        (f"Python {platform.python_version()}", "PSF-2.0",
         _python_license() + "\n\nPython's standard library also bundles zlib, bzip2, xz, libffi, expat and mpdecimal: "
         "their licenses are listed at https://docs.python.org/3/license.html"),
        (ssl.OPENSSL_VERSION, "Apache-2.0" if ssl.OPENSSL_VERSION_INFO[0] >= 3 else "OpenSSL and SSLeay licenses",
         "Python's ssl module. https://www.openssl.org/source/license.html"),
        (f"SQLite {sqlite3.sqlite_version}", "Public domain",
         "Python's sqlite3 module. SQLite is in the public domain: https://www.sqlite.org/copyright.html"),
        ("PyInstaller bootloader", "GPL-2.0-or-later WITH Bootloader-exception",
         "The program that starts the app. Its license allows distributing programs built with it under any license: "
         "https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt"),
    ]
    if system == "win32":
        notices.append(("Microsoft Edge WebView2 SDK (Microsoft.Web.WebView2.Core.dll, "
                        "Microsoft.Web.WebView2.WinForms.dll, WebView2Loader.dll)", "BSD-3-Clause", WEBVIEW2_LICENSE))
    return notices


def notices_text(version: str, system: str = sys.platform) -> str:
    """THIRD_PARTY_NOTICES.txt: every bundled component and Python package with its license, then the license
    texts. The packages are read from this environment, the one PyInstaller bundles (installed from uv.lock)."""
    # Linux leaves pywebview out (openberry.spec), so the desktop extra counts on Windows and macOS only.
    dists = bundled_distributions("openberry", ("desktop",) if system in ("win32", "darwin") else ())
    rule = "=" * 78
    lines = [f"OpenBerry {version}: third-party notices", "",
             "OpenBerry is MIT licensed (LICENSE.txt). The app bundles the software below, each under its own license.",
             "The full license texts follow the list.", "", rule, "Bundled components", rule, ""]
    components = component_notices(system)
    lines += [f"- {name}: {license}" for name, license, _ in components]
    lines += ["", rule, "Python packages", rule, ""]
    lines += [f"- {d.metadata['Name']} {d.version}: {license_name(d)}" for d in dists]
    for name, license, text in components:
        lines += ["", rule, f"{name} ({license})", rule, "", text]
    for dist in dists:
        lines += ["", rule, f"{dist.metadata['Name']} {dist.version} ({license_name(dist)})", rule]
        texts = list(license_texts(dist))
        for file_name, text in texts:
            lines += ["", f"--- {file_name} ---", "", text]
        if not texts:
            lines += ["", "No license file in the package. Its license: " + license_name(dist) + "."]
    return "\n".join(lines) + "\n"


def write_license_files(folder: Path, version: str, system: str = sys.platform, root: Path = ROOT) -> list[Path]:
    """Write LICENSE.txt and THIRD_PARTY_NOTICES.txt into `folder`; returns both paths."""
    folder.mkdir(parents=True, exist_ok=True)
    license_file, notices_file = folder / LICENSE_FILE, folder / NOTICES_FILE
    license_file.write_text((root / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8")
    notices_file.write_text(notices_text(version, system), encoding="utf-8")
    return [license_file, notices_file]


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
    if sys.platform == "darwin":  # next to OpenBerry.app: files added inside it would break its signature
        license_files = write_license_files(dist / "licenses", version)
    else:
        license_files = write_license_files(layout.app, version)
    print(f"Licenses: {', '.join(p.as_posix() for p in license_files)}")
    if not args.no_archive:
        beside = license_files if sys.platform == "darwin" else ()
        archive = make_archive(layout.app, dist / archive_name(platform_tag()), beside=beside)
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
