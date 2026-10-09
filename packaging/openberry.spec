# -*- mode: python -*-
"""PyInstaller spec for the OpenBerry desktop app (one folder, not one file: it starts faster).

Build it with `python packaging/build.py` (or `pyinstaller packaging/openberry.spec`). It makes:

  dist/OpenBerry/            Windows and Linux: the app folder
    OpenBerry[.exe]          the app: a window (Windows, macOS) or the browser (Linux), no console
    openberry-cli[.exe]      the same program with a console: `openberry-cli mcp` for Claude Desktop,
                             and every `openberry` command
    _internal/               Python, the libraries, templates and static files
  dist/OpenBerry.app         macOS: both executables in Contents/MacOS

Both executables run packaging/launcher.py -> openberry.desktop.main. See docs/DESKTOP.md.
"""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# PyInstaller can't cross-build: this is always the machine's own platform. Tests run this file with
# TARGET_PLATFORM set in its namespace to check the macOS and Windows builds from any machine.
TARGET_PLATFORM = globals().get("TARGET_PLATFORM", sys.platform)
IS_WINDOWS = TARGET_PLATFORM == "win32"
IS_MACOS = TARGET_PLATFORM == "darwin"

PACKAGING = Path(SPECPATH)  # noqa: F821 (SPECPATH and the classes below are provided by PyInstaller)
ROOT = PACKAGING.parent
ICONS = PACKAGING / "icons"
APP_NAME = "OpenBerry"
CLI_NAME = "openberry-cli"  # web/pages.py bundled_cli() looks for this name next to the app
BUNDLE_IDENTIFIER = "io.openberry.desktop"
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "openberry" / "__init__.py").read_text()).group(1)

# Package metadata needed at run time. httpx2 and httpcore2 (the MCP SDK's HTTP client) read their
# version with importlib.metadata.version() when imported, and fail without it. PyInstaller 6 finds
# such calls itself; they are listed so the build doesn't depend on that. "openberry" makes
# importlib.metadata.version("openberry") work in the app as it does in a pip install.
METADATA = ("openberry", "httpx2", "httpcore2")

datas = collect_data_files("openberry")  # web/templates and web/static
for distribution in METADATA:
    datas += copy_metadata(distribution)

hiddenimports = [
    *collect_submodules("openberry"),  # imported inside functions: be sure every module is there
    *collect_submodules("uvicorn"),  # loops, protocols and lifespan are imported by name
    *collect_submodules("mcp_types"),
    *collect_submodules("anyio._backends"),  # anyio imports its backend by name
    *collect_submodules("feedparser"),  # RSS/Atom parsing for the news and RSS collectors
]

# Never needed by the app; some are installed next to it in a development environment.
excludes = ["tkinter", "_tkinter", "pytest", "_pytest", "playwright", "PIL", "IPython"]
# pywebview's window: Edge WebView2 through pythonnet on Windows (pyinstaller-hooks-contrib and
# pywebview's own hook add pythonnet's and WebView2's DLLs), WebKit through PyObjC on macOS.
# pywebview imports its backend inside a function; name it, and leave out the ones never used here.
UNUSED_WEBVIEW_BACKENDS = ["webview.platforms.android", "webview.platforms.cef", "webview.platforms.gtk",
                           "webview.platforms.qt"]
if IS_WINDOWS:
    # mshtml stays: pywebview falls back to it without the WebView2 runtime, and desktop.py then opens the browser.
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium", "webview.platforms.mshtml"]
    excludes += UNUSED_WEBVIEW_BACKENDS
elif IS_MACOS:
    hiddenimports += ["webview.platforms.cocoa"]
    excludes += UNUSED_WEBVIEW_BACKENDS
else:
    # Linux: pywebview needs GTK or Qt from the system, which a bundle can't rely on, so the app
    # opens the dashboard in the browser (desktop.open_window falls back when `webview` is missing).
    excludes += ["webview", "bottle", "proxy_tools"]

if IS_WINDOWS:
    icon = str(ICONS / "openberry.ico")
elif IS_MACOS:
    icon = str(ICONS / "openberry.icns")
else:
    icon = None  # Linux executables have no icon

a = Analysis(  # noqa: F821
    [str(PACKAGING / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

common = dict(
    exclude_binaries=True,  # one folder: the libraries go into COLLECT, shared by both executables
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed executables upset antivirus software
    argv_emulation=False,
    target_arch=None,  # this machine's: arm64 on Apple silicon runners
    codesign_identity=None,  # macOS: ad-hoc signature (not notarised, see docs/DESKTOP.md)
    entitlements_file=None,
    icon=icon,
)
app_exe = EXE(pyz, a.scripts, [], name=APP_NAME, console=False, **common)  # noqa: F821
cli_exe = EXE(pyz, a.scripts, [], name=CLI_NAME, console=True, **common)  # noqa: F821

# The app's executable goes first: macOS starts the first one (CFBundleExecutable).
coll = COLLECT(app_exe, cli_exe, a.binaries, a.datas, name=APP_NAME, strip=False, upx=False)  # noqa: F821

if IS_MACOS:
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier=BUNDLE_IDENTIFIER,
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            # COLLECT takes `console` from its last executable (the CLI), and BUNDLE would turn that into
            # LSBackgroundOnly: an app without a Dock icon or a window. It is a normal app.
            "LSBackgroundOnly": False,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.business",
            "NSRequiresAquaSystemAppearance": False,  # follow dark mode
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},  # the window loads http://127.0.0.1
            "NSHumanReadableCopyright": "MIT licensed. Not affiliated with Gojiberry.",
        },
    )
