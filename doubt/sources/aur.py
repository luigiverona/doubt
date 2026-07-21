from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallItem, InstallResult
from ..system.run import CommandRunner
from . import pacman

YAY_REPO = "https://aur.archlinux.org/yay.git"
AUR_BOOTSTRAP_DEPS = ("git", "base-devel")


def install(
    packages: Sequence[InstallItem],
    runner: CommandRunner,
    pacman_deps: Sequence[str] = (),
) -> list[InstallResult]:
    if not packages:
        return []

    ensure_bootstrap_deps(pacman_deps, runner)
    helper_status = ensure_yay(runner)

    missing: list[InstallItem] = []
    statuses: dict[InstallItem, str] = {}
    for package in packages:
        if is_installed(package.name, runner):
            statuses[package] = "ok"
        else:
            statuses[package] = "add"
            missing.append(package)

    helper = InstallResult("yay", "pacman deps", "bootstrap", helper_status)
    if not missing:
        return [helper, *results_for(packages, statuses)]

    try:
        build_root = _build_root(runner)
        runner.run(
            [
                "yay",
                "--builddir",
                str(build_root),
                "-S",
                "--needed",
                "--",
                *[package.name for package in missing],
            ],
            quiet=True,
        )
    except OperationalError as error:
        if error.kind is not FailureKind.COMMAND_FAILURE:
            raise
        raise OperationalError(
            FailureKind.PACKAGE_INSTALLATION_FAILURE,
            "aur",
            f"failed to install AUR applications: {error}",
        ) from error
    if not runner.dry_run:
        verify_installed(missing, statuses, runner)
    return [helper, *results_for(packages, statuses)]


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


def ensure_bootstrap_deps(pacman_deps: Sequence[str], runner: CommandRunner) -> None:
    listed = set(pacman_deps)
    missing = [package for package in AUR_BOOTSTRAP_DEPS if package not in listed]
    if missing:
        missing_text = ", ".join(missing)
        raise OperationalError(
            FailureKind.BLOCKED_PRECONDITION,
            "aur",
            f"AUR requires {missing_text} in deps/pacman/bootstrap",
        )

    if runner.dry_run:
        return

    if not runner.command_exists("git"):
        raise OperationalError(
            FailureKind.UNAVAILABLE_EXECUTABLE,
            "aur",
            "AUR requires git, but git is unavailable after pacman deps",
        )
    if not pacman.is_installed("base-devel", runner):
        raise OperationalError(
            FailureKind.BLOCKED_PRECONDITION,
            "aur",
            "AUR requires base-devel, but base-devel is unavailable after pacman deps",
        )


def ensure_yay(runner: CommandRunner) -> str:
    if runner.command_exists("yay"):
        return "ok"

    if runner.dry_run:
        return "add"

    try:
        checkout = _build_root(runner) / "yay"
        runner.run(["git", "clone", YAY_REPO, str(checkout)], quiet=True)
        runner.run(["makepkg", "-si"], cwd=checkout, quiet=True)
    except OperationalError as error:
        if error.kind is not FailureKind.COMMAND_FAILURE:
            raise
        raise OperationalError(
            FailureKind.PACKAGE_INSTALLATION_FAILURE,
            "aur",
            f"failed to bootstrap the AUR helper: {error}",
        ) from error

    if not runner.command_exists("yay"):
        raise OperationalError(
            FailureKind.PACKAGE_INSTALLATION_FAILURE,
            "aur",
            "yay bootstrap completed, but yay is still unavailable",
        )
    return "add"


def _build_root(runner: CommandRunner) -> Path:
    value = getattr(runner, "environment", {}).get("DOUBT_WORK_ROOT")
    if not value:
        raise OperationalError(
            FailureKind.UNSAFE_PATH,
            "aur",
            "AUR installation requires the private doubt temporary root",
        )
    root = Path(value) / "aur"
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise OperationalError(FailureKind.UNSAFE_PATH, "aur", "unsafe AUR build root")
    return root


def is_installed(package: str, runner: CommandRunner) -> bool:
    if not runner.command_exists("yay"):
        return False
    return runner.succeeds(["yay", "-Q", package])
