"""Detect dangerous shell use and execution-boundary regressions."""

from __future__ import annotations

import ast
import re
import sys

from . import ROOT

SHELL_PATTERNS = {
    "eval": re.compile(r"(^|[;&|]\s*)eval\s"),
    "remote shell pipe": re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba)?sh\b"),
    "destructive root removal": re.compile(r"\brm\s+-[A-Za-z]*r[A-Za-z]*f[A-Za-z]*\s+/(?:\s|$)"),
    "privilege escalation": re.compile(r"(^|[;&|]\s*)sudo\s"),
}


def subprocess_violations() -> list[str]:
    errors: list[str] = []
    for path in sorted((ROOT / "doubt").rglob("*.py")):
        relative = path.relative_to(ROOT / "doubt")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        uses_subprocess = any(
            (isinstance(node, ast.Import) and any(alias.name == "subprocess" for alias in node.names))
            or (isinstance(node, ast.ImportFrom) and node.module == "subprocess")
            for node in ast.walk(tree)
        )
        if uses_subprocess and relative.as_posix() != "system/run.py":
            errors.append(f"unexpected subprocess import: doubt/{relative}")
    return errors


def shell_violations() -> list[str]:
    errors: list[str] = []
    paths = [ROOT / "install", ROOT / "check"]
    paths.extend([ROOT / "bootstrap" / "install", ROOT / "release" / "build", ROOT / "release" / "publish"])
    paths.extend(sorted((ROOT / ".github" / "workflows").glob("*.yml")))
    paths.extend(
        path
        for path in sorted((ROOT / "acceptance").rglob("*"))
        if path.is_file() and (path.name == "Containerfile" or path.suffix == "")
    )
    for path in paths:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        for name, pattern in SHELL_PATTERNS.items():
            for match in pattern.finditer(content):
                if (
                    name == "remote shell pipe"
                    and path == ROOT / "acceptance/container/check"
                    and "curl -fsSL http://127.0.0.1:$PORT/install | bash" in match.group()
                    and "DOUBT_ACCEPTANCE_BASE_URL" in content
                    and "/run/doubt-disposable-acceptance" in content
                ):
                    continue
                line = content.count("\n", 0, match.start()) + 1
                errors.append(f"{path.relative_to(ROOT)}:{line}: {name}")
    return errors


def main() -> int:
    errors = subprocess_violations() + shell_violations()
    if errors:
        print("dangerous execution audit failed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("dangerous execution audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
