"""Reject stale shell contracts in executable code and current documentation."""

from __future__ import annotations

import re
import sys

from . import ROOT

REMOVED = (
    "#!" + "/bin/sh",
    "#!" + "/usr/bin/env sh",
)
UNSUPPORTED_INSTALL = re.compile(r"\|\s*sh(?:\s|$)|\bsh\s+/tmp/doubt-install\b")
PORTABILITY_CLAIM = re.compile(r"\bPOSIX(?:-compatible)? shell\b", re.IGNORECASE)
EXCLUDED = {
    ROOT / "CHANGELOG.md",
    ROOT / "quality" / "terms.py",
}


def violations() -> list[str]:
    errors: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "tests" in path.parts:
            continue
        if path in EXCLUDED:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, 1):
            if any(value in line for value in REMOVED):
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
            elif UNSUPPORTED_INSTALL.search(line) or PORTABILITY_CLAIM.search(line):
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("stale terminology found:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("terminology check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
