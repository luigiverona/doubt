from __future__ import annotations

from collections.abc import Sequence

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallItem, InstallResult
from ..system.run import CommandRunner

FLATHUB_NAME = "flathub"
FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
FLATPAK_BOOTSTRAP_DEPS = ("flatpak",)


def install(
    app_ids: Sequence[InstallItem],
    runner: CommandRunner,
    pacman_deps: Sequence[str] = (),
) -> list[InstallResult]:
    if not app_ids:
        return []

    ensure_bootstrap_deps(pacman_deps, runner)
    ensure_flathub(runner)

    missing: list[InstallItem] = []
    statuses: dict[InstallItem, str] = {}
    for app_id in app_ids:
        if is_installed(app_id.name, runner):
            statuses[app_id] = "ok"
        else:
            statuses[app_id] = "add"
            missing.append(app_id)

    if not missing:
        return results_for(app_ids, statuses)

    try:
        runner.run(
            ["flatpak", "install", "--assumeyes", FLATHUB_NAME, *[app_id.name for app_id in missing]],
            quiet=True,
        )
    except OperationalError as error:
        if error.kind is not FailureKind.COMMAND_FAILURE:
            raise
        raise OperationalError(
            FailureKind.FLATPAK_FAILURE,
            "flatpak",
            f"failed to install Flatpak applications: {error}",
        ) from error
    if not runner.dry_run:
        verify_installed(missing, statuses, runner)
    return results_for(app_ids, statuses)


def verify_installed(
    app_ids: Sequence[InstallItem],
    statuses: dict[InstallItem, str],
    runner: CommandRunner,
) -> None:
    for app_id in app_ids:
        if not is_installed(app_id.name, runner):
            statuses[app_id] = "fail"


def results_for(
    app_ids: Sequence[InstallItem],
    statuses: dict[InstallItem, str],
) -> list[InstallResult]:
    return [
        InstallResult(
            name=app_id.name,
            source=app_id.source,
            category=app_id.category,
            status=statuses[app_id],
        )
        for app_id in app_ids
    ]


def ensure_bootstrap_deps(pacman_deps: Sequence[str], runner: CommandRunner) -> None:
    listed = set(pacman_deps)
    missing = [package for package in FLATPAK_BOOTSTRAP_DEPS if package not in listed]
    if missing:
        missing_text = ", ".join(missing)
        raise OperationalError(
            FailureKind.BLOCKED_PRECONDITION,
            "flatpak",
            f"Flatpak requires {missing_text} in deps/pacman/bootstrap",
        )

    if runner.dry_run:
        return

    if not runner.command_exists("flatpak"):
        raise OperationalError(
            FailureKind.UNAVAILABLE_EXECUTABLE,
            "flatpak",
            "Flatpak requires flatpak, but flatpak is unavailable after pacman deps",
        )


def ensure_flathub(runner: CommandRunner) -> None:
    if has_flathub(runner):
        return
    try:
        runner.run(
            ["flatpak", "remote-add", "--if-not-exists", FLATHUB_NAME, FLATHUB_URL],
            quiet=True,
        )
    except OperationalError as error:
        if error.kind is not FailureKind.COMMAND_FAILURE:
            raise
        raise OperationalError(
            FailureKind.FLATPAK_FAILURE,
            "flatpak",
            f"failed to configure Flathub: {error}",
        ) from error


def has_flathub(runner: CommandRunner) -> bool:
    if not runner.command_exists("flatpak"):
        return False
    remotes = runner.output(["flatpak", "remotes", "--columns=name"])
    return FLATHUB_NAME in {line.strip() for line in remotes.splitlines()}


def is_installed(app_id: str, runner: CommandRunner) -> bool:
    if not runner.command_exists("flatpak"):
        return False
    return runner.succeeds(["flatpak", "info", app_id])
