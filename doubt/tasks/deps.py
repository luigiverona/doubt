"""Dependency reconciliation task."""

from __future__ import annotations

from collections.abc import Sequence

from ..core.result import InstallItem, InstallResult
from ..packages.lists import PackageList
from ..sources import pacman
from ..system.run import CommandRunner


def run(
    package_lists: Sequence[PackageList],
    runner: CommandRunner,
    category: str | None = None,
) -> list[InstallResult]:
    selected = [item for item in package_lists if category is None or item.category == category]
    items = [
        InstallItem(name, "pacman deps", item.category)
        for item in selected
        if item.source == "pacman"
        for name in item.apps
    ]
    label = "Codex dependencies" if category == "codex" else "pacman dependencies"
    return pacman.install(items, runner, label=label)
