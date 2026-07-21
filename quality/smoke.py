"""Run non-mutating public-interface smoke checks."""

from __future__ import annotations

import subprocess
import sys

from . import ROOT


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


def main() -> int:
    expected_help = "usage:\n  ./install\n  ./install plan\n  ./install verify"
    for command in (["./install", "--help"], [sys.executable, "-m", "doubt", "--help"]):
        result = run(command)
        if result.returncode != 0 or expected_help not in result.stdout or result.stderr:
            print(f"CLI help smoke failed: {' '.join(command)}", file=sys.stderr)
            return 1
    for removed in ("tasks", "help", "--only", "--except", "--details", "--dry-run"):
        result = run(["./install", removed])
        if result.returncode != 2 or result.stdout or "error:" not in result.stderr:
            print(f"removed interface was not rejected: {removed}", file=sys.stderr)
            return 1
    result = run(["./install", "-h"])
    if result.returncode != 2 or result.stdout or result.stderr != "error: unrecognized argument: -h\n":
        print("short help rejection smoke failed", file=sys.stderr)
        return 1
    print("safe CLI smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
