"""Application execution requests and task selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .task import SUPPORTED_TASKS, TASK_ORDER


class Mode(StrEnum):
    MUTATE = "mutate"
    PLAN = "plan"
    VERIFY = "verify"


@dataclass(frozen=True)
class Request:
    mode: Mode = Mode.MUTATE
    selected: frozenset[str] | None = None
    excluded: frozenset[str] | None = None
    details: bool = False
    apps: Path = Path("apps")
    deps: Path = Path("deps")
    installed: bool = False


@dataclass(frozen=True)
class PackageRequest:
    action: str
    source: str | None = None
    category: str | None = None
    package: str | None = None
    dry_run: bool = False


def resolve(
    selected: frozenset[str] | None = None,
    excluded: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if selected is not None and excluded is not None:
        raise ValueError("--only and --except cannot be used together.")
    resolved = set(TASK_ORDER if selected is None else selected)
    if excluded is not None:
        resolved -= excluded
    return tuple(task for task in TASK_ORDER if task in resolved)


def parse(value: str) -> frozenset[str]:
    entries = [entry.strip() for entry in value.split(",")]
    if not entries or any(not entry for entry in entries):
        raise ValueError("task list must not contain empty entries.")
    tasks = frozenset(entries)
    unknown = sorted(tasks - SUPPORTED_TASKS)
    if unknown:
        names = ", ".join(unknown)
        available = ", ".join(TASK_ORDER)
        noun = "task" if len(unknown) == 1 else "tasks"
        raise ValueError(f"unknown {noun}: {names}\navailable tasks: {available}")
    return tasks
