"""Print one version's section of CHANGELOG.md: `python packaging/release_notes.py v0.2.0 > notes.md`.

The release job (.github/workflows/desktop.yml) runs it for the pushed tag and creates the release with
`gh release create --notes-file`, so the release notes say what changed and how to upgrade, not only the titles of the
merged pull requests (GitHub adds those below). A version with no section stops the release. Standard library only:
the release job runs it with the runner's own Python, before anything is installed.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
# "## 0.2.0", "## v0.2.0", "## [0.2.0] - 2026-10-11", "## 0.2.0 (October 2026)"
HEADING = re.compile(r"^## \[?v?(?P<version>\d+\.\d+\.\d+)\]?(?:\s.*)?$")


# A line that starts its own block: a heading, a list item, a table row, a quote or a code fence.
BLOCK_START = re.compile(r"^\s*(?:#|[-*+]\s|\d+[.)]\s|\||>|```)")


class NoSection(Exception):
    pass


def unwrap(text: str) -> str:
    """Join hard-wrapped lines: GitHub shows every line break in release notes, so a wrapped paragraph would show
    as short broken lines. Headings, list items, tables, quotes and code blocks keep their own lines."""
    out: list[str] = []
    in_code = False
    for line in text.splitlines():
        fence = line.lstrip().startswith("```")
        if in_code or fence or not line.strip() or not out or not out[-1].strip() or BLOCK_START.match(line) \
                or out[-1].lstrip().startswith(("#", "|", "```")):
            out.append(line.rstrip())
        else:
            out[-1] = out[-1].rstrip() + " " + line.strip()
        if fence:
            in_code = not in_code
    return "\n".join(out)


def section(text: str, version: str) -> str:
    """The text under `## <version>` up to the next `## ` heading, without that heading, unwrapped. Raises
    NoSection."""
    version = version.strip().removeprefix("v")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if (match := HEADING.match(line.rstrip())) and match["version"] == version), None)
    if start is None:
        raise NoSection(f"CHANGELOG.md has no section for {version}: add a '## {version}' heading with what changed")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = unwrap("\n".join(lines[start + 1:end])).strip()
    if not body:
        raise NoSection(f"CHANGELOG.md's section for {version} is empty")
    return body + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path = CHANGELOG
    if len(args) == 3 and args[1] == "--changelog":
        path = Path(args[2])
        args = args[:1]
    if len(args) != 1:
        print("usage: python packaging/release_notes.py vX.Y.Z [--changelog CHANGELOG.md]", file=sys.stderr)
        return 2
    try:
        notes = section(path.read_text(encoding="utf-8"), args[0])
    except (OSError, NoSection) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
