"""Scan repository text for high-confidence credential material."""

from __future__ import annotations

import re
import sys

from . import ROOT

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"),
    "OpenAI token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


def violations() -> list[str]:
    errors: list[str] = []
    excluded = {ROOT / "quality" / "secrets.py"}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path in excluded:
            continue
        relative = path.relative_to(ROOT)
        if ".git" in relative.parts or any(part.startswith(".") and part.endswith("_cache") for part in relative.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                line = content.count("\n", 0, match.start()) + 1
                errors.append(f"{relative}:{line}: possible {name}")
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("secret scan failed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("repository secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
