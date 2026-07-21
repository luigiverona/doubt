"""Reject generated validation artifacts inside the repository."""

from __future__ import annotations

import sys

from . import ROOT

FORBIDDEN_DIRECTORIES = {
    "__pycache__",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
}
FORBIDDEN_FILES = {".coverage", ".DS_Store", "Thumbs.db", "coverage.json", "coverage.xml"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}


def violations() -> list[str]:
    errors: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if ".git" in relative.parts:
            continue
        if path.is_dir() and path.name in FORBIDDEN_DIRECTORIES:
            errors.append(str(relative))
        elif path.is_file() and (path.suffix in FORBIDDEN_SUFFIXES or path.name in FORBIDDEN_FILES):
            errors.append(str(relative))
        elif path.is_file() and path.name.startswith("doubt-") and path.name.endswith(".tar.gz"):
            errors.append(str(relative))
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("generated artifacts found:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("generated-artifact check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
