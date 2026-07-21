"""Canonical task registration and order."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    description: str


TASKS = (
    Task("deps", "package dependencies"),
    Task("apps", "workstation applications"),
    Task("github", "GitHub CLI and Git protocol"),
    Task("ssh", "managed SSH identity and GitHub key"),
    Task("git", "managed Git configuration"),
    Task("codex", "dual-account Codex configuration"),
    Task("path", "Fish launcher PATH integration"),
    Task("verify", "strict managed-state audit"),
)
TASK_ORDER = tuple(task.name for task in TASKS)
SUPPORTED_TASKS = frozenset(TASK_ORDER)
