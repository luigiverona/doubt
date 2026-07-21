"""Enforce repository-owned source and test naming policy."""

from __future__ import annotations

import re
import sys

from . import ROOT

WORD = re.compile(r"^[a-z]+$")
PYTHON_EXCEPTIONS = {"__init__", "__main__"}
FORBIDDEN = {"common", "helpers", "manager", "misc", "shared", "utils"}


def violations() -> list[str]:
    errors: list[str] = []
    for base_name in ("acceptance", "doubt", "tests", "quality"):
        base = ROOT / base_name
        for path in sorted(base.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT)
            if path.is_dir() and not WORD.fullmatch(path.name):
                errors.append(f"directory name is not one lowercase word: {relative}")
            if path.suffix == ".py":
                stem = path.stem
                if stem not in PYTHON_EXCEPTIONS and not WORD.fullmatch(stem):
                    errors.append(f"Python module name is not one lowercase word: {relative}")
                if stem in FORBIDDEN:
                    errors.append(f"forbidden catch-all Python module: {relative}")
            elif path.is_file() and path.name != "Containerfile" and "." not in path.name:
                if not WORD.fullmatch(path.name):
                    errors.append(f"script name is not one lowercase word: {relative}")

    workflows = ROOT / ".github" / "workflows"
    if workflows.exists():
        for path in sorted(workflows.iterdir()):
            if path.is_file() and (path.suffix != ".yml" or not WORD.fullmatch(path.stem)):
                errors.append(f"workflow filename must be one lowercase word: {path.relative_to(ROOT)}")
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("naming policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
