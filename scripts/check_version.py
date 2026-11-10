"""Release helper (no third-party imports).

    python scripts/check_version.py v0.2.0          # prints 0.2.0, exit 1 if it differs from tgdl.__version__
    python scripts/check_version.py v0.2.0 --notes  # prints that version's section of CHANGELOG.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_version() -> str:
    text = (ROOT / "tgdl" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("__version__ not found in tgdl/__init__.py")
    return match.group(1)


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    body = match.group(1).strip() if match else ""
    return body or f"Telegram Downloader {version}."


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    tag = argv[0].strip()
    version = tag[1:] if tag.startswith("v") else tag
    expected = package_version()
    if version != expected:
        print(f"Tag {tag} does not match tgdl.__version__ = {expected}", file=sys.stderr)
        return 1
    if "--notes" in argv[1:]:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        print(changelog_section(version))
    else:
        print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
