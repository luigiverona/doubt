"""Enforce separate statement, branch, and critical-module coverage gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict, cast

CRITICAL = {
    "doubt/app.py": 80.0,
    "doubt/cli.py": 80.0,
    "doubt/core/plan.py": 80.0,
    "doubt/core/result.py": 80.0,
    "doubt/core/task.py": 80.0,
    "doubt/system/run.py": 80.0,
    "doubt/system/files.py": 80.0,
    "doubt/system/lock.py": 85.0,
    "doubt/packages/resolve.py": 80.0,
    "doubt/packages/edit.py": 85.0,
    "doubt/packages/lists.py": 85.0,
    "doubt/tasks/verify.py": 80.0,
    "doubt/tasks/codex.py": 80.0,
    "doubt/ui/prompt.py": 80.0,
}


class Summary(TypedDict):
    covered_lines: int
    num_statements: int
    covered_branches: int
    num_branches: int


def percentage(covered: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * covered / total


def combined(summary: Summary) -> float:
    return percentage(
        summary["covered_lines"] + summary["covered_branches"],
        summary["num_statements"] + summary["num_branches"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--statements", type=float, default=90.0)
    parser.add_argument("--branches", type=float, default=85.0)
    parser.add_argument("--critical", type=float, default=80.0)
    args = parser.parse_args(argv)
    data = json.loads(args.report.read_text(encoding="utf-8"))
    totals = cast(Summary, data["totals"])
    statement_coverage = percentage(totals["covered_lines"], totals["num_statements"])
    branch_coverage = percentage(totals["covered_branches"], totals["num_branches"])
    errors: list[str] = []
    if statement_coverage < args.statements:
        errors.append(f"statement coverage {statement_coverage:.2f}% is below {args.statements:.2f}%")
    if branch_coverage < args.branches:
        errors.append(f"branch coverage {branch_coverage:.2f}% is below {args.branches:.2f}%")
    critical_values: dict[str, float] = {}
    lock_statement_coverage = 0.0
    for name, floor in CRITICAL.items():
        summary = cast(Summary, data["files"][name]["summary"])
        value = combined(summary)
        threshold = max(args.critical, floor)
        critical_values[name] = value
        if value < threshold:
            errors.append(f"critical module {name} coverage {value:.2f}% is below {threshold:.2f}%")
        if name == "doubt/system/lock.py":
            lock_statement_coverage = percentage(summary["covered_lines"], summary["num_statements"])
            if lock_statement_coverage < 85.0:
                errors.append(f"mutation-lock statement coverage {lock_statement_coverage:.2f}% is below 85.00%")
    print(f"statement coverage: {statement_coverage:.2f}%")
    print(f"branch coverage: {branch_coverage:.2f}%")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    lowest_name, lowest_value = min(critical_values.items(), key=lambda item: item[1])
    print(f"lowest critical-module coverage: {lowest_name} {lowest_value:.2f}%")
    print(f"mutation-lock statement coverage: {lock_statement_coverage:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
