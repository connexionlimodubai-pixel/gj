"""Entry point of the packaged OpenBerry app: PyInstaller builds both executables from this file.

`OpenBerry` (windowed) and `openberry-cli` (console) both run `openberry.desktop.main`, which opens
the app, or runs a command-line command when one is given (`openberry-cli mcp` is what Claude
Desktop starts). See packaging/openberry.spec and docs/DESKTOP.md.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping

# PyInstaller's bootloader on Linux and other Unixes (not macOS) puts the bundle's library folder
# first in LD_LIBRARY_PATH, and keeps the original value in LD_LIBRARY_PATH_ORIG.
LIBRARY_PATH = "LD_LIBRARY_PATH"
ORIGINAL_SUFFIX = "_ORIG"


def restore_library_path(environ: MutableMapping[str, str] | None = None, *, frozen: bool | None = None,
                         platform: str | None = None) -> None:
    """Give programs the app starts their own system libraries back.

    With the bundle's folder in LD_LIBRARY_PATH, the browser that `xdg-open` starts (the Linux app
    opens the dashboard in it) would load OpenBerry's copies of libssl, libz and the like, and may
    crash. This process is unaffected: the loader read LD_LIBRARY_PATH when it started.
    """
    environ = os.environ if environ is None else environ
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    if not frozen or platform in {"win32", "cygwin", "darwin"}:
        return
    original = environ.pop(LIBRARY_PATH + ORIGINAL_SUFFIX, None)
    if original:
        environ[LIBRARY_PATH] = original
    else:  # it was unset before the app started
        environ.pop(LIBRARY_PATH, None)


def main() -> None:
    restore_library_path()
    from openberry.desktop import main as desktop_main

    desktop_main()


if __name__ == "__main__":
    main()
