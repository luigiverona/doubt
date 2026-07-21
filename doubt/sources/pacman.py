from __future__ import annotations

from collections.abc import Sequence

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallItem, InstallResult
from ..system.run import CommandRunner


def install(
    packages: Sequence[InstallItem],
    runner: CommandRunner,
    label: str = "pacman",
) -> list[InstallResult]:
    missing: list[InstallItem] = []
    statuses: dict[InstallItem, str] = {}
    for package in packages:
        if is_installed(package.name, runner):
            statuses[package] = "ok"
        else:
            statuses[package] = "add"
            missing.append(package)

    if not missing:
        return results_for(packages, statuses)

    if not runner.dry_run and not runner.command_exists("pacman"):
        raise OperationalError(
            FailureKind.UNAVAILABLE_EXECUTABLE,
            "pacman",
            "pacman is required to install pacman packages; doubt targets Arch Linux and Arch-based distros",
        )

    try:
        runner.run(
            ["sudo", "pacman", "-S", "--needed", "--", *[package.name for package in missing]],
            quiet=True,
        )
    except OperationalError as error:
        if error.kind is not FailureKind.COMMAND_FAILURE:
            raise
        raise OperationalError(
            FailureKind.PACKAGE_INSTALLATION_FAILURE,
            label,
            f"failed to install {label}: {error}",
        ) from error
    if not runner.dry_run:
        verify_installed(missing, statuses, runner)
    return results_for(packages, statuses)


def verify_installed(
    packages: Sequence[InstallItem],
    statuses: dict[InstallItem, str],
    runner: CommandRunner,
) -> None:
    for package in packages:
        if not is_installed(package.name, runner):
            statuses[package] = "fail"


def results_for(
    packages: Sequence[InstallItem],
    statuses: dict[InstallItem, str],
) -> list[InstallResult]:
    return [
        InstallResult(
            name=package.name,
            source=package.source,
            category=package.category,
            status=statuses[package],
        )
        for package in packages
    ]


def is_installed(package: str, runner: CommandRunner) -> bool:
    if not runner.command_exists("pacman"):
        return False
    return runner.succeeds(["pacman", "-Qi", package])
