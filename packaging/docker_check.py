"""Check that the Docker image holds exactly the locked packages: `python packaging/docker_check.py IMAGE`.

CI runs it after building the image (.github/workflows/docker.yml). It compares `uv export --frozen` (the versions in
uv.lock, which the tests run with) with the packages installed in the image, and fails on any difference. The
image's own Python decides which locked packages apply to it (environment markers such as sys_platform).
Needs uv and docker.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence

EXPORT = ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "--no-header",
          "--no-annotate"]
# Installed in every venv, or the project itself: not in the export.
NOT_LOCKED = {"openberry", "pip", "setuptools", "wheel"}

# Runs inside the image: the locked lines on stdin, JSON on stdout.
IN_IMAGE = r"""
import importlib.metadata, json, re, sys
try:
    from packaging.markers import Marker
except ImportError:
    from pip._vendor.packaging.markers import Marker
norm = lambda name: re.sub(r"[-_.]+", "-", name).lower()
wanted = {}
for line in sys.stdin:
    line = line.split("#", 1)[0].strip()
    if not line:
        continue
    requirement, _, marker = line.partition(";")
    name, _, version = requirement.strip().partition("==")
    if not marker.strip() or Marker(marker.strip()).evaluate():
        wanted[norm(name)] = version.strip()
installed = {norm(d.metadata["Name"]): d.version for d in importlib.metadata.distributions()}
print(json.dumps({"wanted": wanted, "installed": installed}))
"""


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def differences(wanted: dict[str, str], installed: dict[str, str]) -> list[str]:
    """What differs between the locked packages that apply to the image and the ones installed in it."""
    installed = {normalize(n): v for n, v in installed.items() if normalize(n) not in NOT_LOCKED}
    wanted = {normalize(n): v for n, v in wanted.items()}
    problems = [f"{name}: locked {version}, installed {installed[name]}"
                for name, version in sorted(wanted.items()) if name in installed and installed[name] != version]
    problems += [f"{name} {version} is locked but not installed"
                 for name, version in sorted(wanted.items()) if name not in installed]
    problems += [f"{name} {version} is installed but not in uv.lock"
                 for name, version in sorted(installed.items()) if name not in wanted]
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python packaging/docker_check.py IMAGE", file=sys.stderr)
        return 2
    locked = subprocess.run(EXPORT, check=True, capture_output=True, text=True).stdout
    found = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "python", args[0], "-c", IN_IMAGE],
                           input=locked, check=True, capture_output=True, text=True).stdout
    data = json.loads(found)
    problems = differences(data["wanted"], data["installed"])
    if problems:
        print("The image does not match uv.lock:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(f"The image holds exactly the {len(data['wanted'])} locked packages that apply to it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
