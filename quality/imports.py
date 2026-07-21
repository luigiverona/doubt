"""Import every production module without import-time output."""

from __future__ import annotations

import importlib
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from . import ROOT


def module_names() -> tuple[str, ...]:
    names: list[str] = []
    for path in sorted((ROOT / "doubt").rglob("*.py")):
        parts = list(path.with_suffix("").relative_to(ROOT).parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return tuple(names)


def main() -> int:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        for name in module_names():
            importlib.import_module(name)
    if stdout.getvalue() or stderr.getvalue():
        print("production imports emitted output", file=sys.stderr)
        return 1
    print(f"imported {len(module_names())} production modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
