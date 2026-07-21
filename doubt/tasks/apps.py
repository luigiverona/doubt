"""Application reconciliation task."""

from __future__ import annotations

from collections.abc import Sequence

from ..core.result import InstallItem, InstallResult
from ..packages.lists import PackageList
from ..sources import aur, flatpak, pacman
from ..system.run import CommandRunner


def run(
    app_lists: Sequence[PackageList],
    dependency_lists: Sequence[PackageList],
    runner: CommandRunner,
) -> list[InstallResult]:
    bootstrap = tuple(
        name
        for item in dependency_lists
        if item.source == "pacman" and item.category == "bootstrap"
        for name in item.apps
    )

    def items(source: str, label: str) -> list[InstallItem]:
        return [
            InstallItem(name, label, item.category) for item in app_lists if item.source == source for name in item.apps
        ]

    results = pacman.install(items("pacman", "pacman apps"), runner, label="pacman apps")
    results.extend(aur.install(items("aur", "aur"), runner, pacman_deps=bootstrap))
    results.extend(flatpak.install(items("flatpak", "flatpak"), runner, pacman_deps=bootstrap))
    return results
