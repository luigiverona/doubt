"""Post-confirmation activation of a verified staged release and default state."""

from __future__ import annotations

import filecmp
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.version import VERSION
from . import files, runtime

BOOTSTRAP_VARIABLE = "DOUBT_BOOTSTRAP_STAGE"
SHA_VARIABLE = "DOUBT_BOOTSTRAP_SHA256"
MARKER = ".doubt-release"
LAUNCHER_MARKER = "# Managed by doubt. Do not edit."
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class ActivationPlan:
    source: Path | None
    release: bool
    declarations: bool
    launcher: bool

    @property
    def required(self) -> int:
        return sum((self.release, self.declarations, self.launcher))


def inspect(environment: Mapping[str, str] | None = None) -> ActivationPlan:
    values = os.environ if environment is None else environment
    source_value = values.get(BOOTSTRAP_VARIABLE)
    source = Path(source_value) if source_value else None
    if source is not None:
        _validate_source(source, values)
    elif not runtime.frozen():
        return ActivationPlan(None, False, False, False)

    paths = _paths(values)
    if source is not None:
        _reject_downgrade(paths.current)
    release_required = source is not None and not _trusted_release(paths.release, source)
    declarations_required = not paths.packages.exists()
    if not declarations_required:
        _validate_packages(paths.packages)
    launcher_required = source is not None and not _managed_launcher(paths.launcher)
    return ActivationPlan(source, release_required, declarations_required, launcher_required)


def apply(plan: ActivationPlan, environment: Mapping[str, str] | None = None) -> ActivationPlan:
    values = os.environ if environment is None else environment
    paths = _paths(values)
    active_release = paths.release
    if plan.source is not None:
        active_release = _install_release(plan.source, paths.release, values)
    if plan.declarations:
        _materialize_defaults(paths.packages)
    if plan.source is not None:
        _activate_current(paths.current, active_release)
        _install_launcher(paths.launcher)
    return inspect(values)


@dataclass(frozen=True)
class _Paths:
    data: Path
    release: Path
    current: Path
    packages: Path
    launcher: Path


def _paths(values: Mapping[str, str]) -> _Paths:
    home_value = values.get("HOME")
    if not home_value or not Path(home_value).is_absolute():
        raise _failure("HOME must be an absolute path")
    home = Path(home_value)
    data_home = Path(values.get("XDG_DATA_HOME", str(home / ".local/share")))
    config_home = Path(values.get("XDG_CONFIG_HOME", str(home / ".config")))
    if not data_home.is_absolute() or not config_home.is_absolute():
        raise _failure("XDG data and configuration paths must be absolute")
    data = data_home / "doubt"
    return _Paths(
        data,
        data / "releases" / VERSION,
        data / "current",
        config_home / "doubt" / "packages",
        home / ".local/bin/doubt",
    )


def _validate_source(source: Path, values: Mapping[str, str]) -> None:
    if not source.is_absolute() or source == Path("/"):
        raise _failure("staged release path is invalid")
    expected_sha = values.get(SHA_VARIABLE, "")
    marker = source / MARKER
    if not expected_sha or len(expected_sha) != 64 or any(value not in "0123456789abcdef" for value in expected_sha):
        raise _failure("staged release digest is invalid")
    if not source.is_dir() or source.is_symlink() or not marker.is_file() or marker.is_symlink():
        raise _failure("staged release is incomplete or unsafe")
    expected = f"version={VERSION}\nsha256={expected_sha}\n"
    if marker.read_text(encoding="utf-8") != expected:
        raise _failure("staged release metadata does not match the verified artifact")
    _validate_tree(source)


def _validate_tree(root: Path) -> None:
    uid = os.getuid()
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        if metadata.st_uid != uid or stat.S_ISLNK(metadata.st_mode):
            raise _failure("staged release contains an unsafe owner or link")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise _failure("staged release contains an unsupported file type")


def _trusted_release(target: Path, source: Path) -> bool:
    return target.is_dir() and not target.is_symlink() and _trees_equal(source, target)


def _reject_downgrade(current: Path) -> None:
    active = _active_version(current)
    requested = _version_tuple(VERSION)
    if active is not None and active > requested:
        active_text = ".".join(str(value) for value in active)
        raise _failure(f"downgrade from Doubt {active_text} to {VERSION} is not supported")


def _active_version(current: Path) -> tuple[int, int, int] | None:
    if not current.is_symlink():
        return None
    link = current.readlink()
    if link.is_absolute() or len(link.parts) != 2 or link.parts[0] != "releases":
        return None
    target = current.parent / link
    marker = target / MARKER
    if target.is_symlink() or not target.is_dir() or marker.is_symlink() or not marker.is_file():
        return None
    try:
        fields = dict(line.split("=", 1) for line in marker.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    version = fields.get("version")
    return _version_tuple(version) if version is not None and SEMVER.fullmatch(version) else None


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if match is None:
        raise _failure("Doubt release version is invalid")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _trees_equal(left: Path, right: Path) -> bool:
    comparison = filecmp.dircmp(left, right)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    if any(not filecmp.cmp(left / name, right / name, shallow=False) for name in comparison.common_files):
        return False
    return all(_trees_equal(left / name, right / name) for name in comparison.common_dirs)


def _install_release(source: Path, primary: Path, values: Mapping[str, str]) -> Path:
    if _trusted_release(primary, source):
        return primary
    digest = values[SHA_VARIABLE]
    target = primary if not primary.exists() else primary.with_name(f"{VERSION}-{digest[:12]}")
    if target.exists():
        if _trusted_release(target, source):
            return target
        raise _failure("an unsafe same-version release path blocks repair")
    releases = primary.parent
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{VERSION}.stage.", dir=releases))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True, copy_function=shutil.copy2)
        _validate_tree(temporary)
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


def _materialize_defaults(target: Path) -> None:
    if target.exists():
        _validate_packages(target)
        return
    parent = target.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".packages.stage.", dir=parent))
    try:
        shutil.copytree(runtime.resource_root() / "apps", temporary / "apps")
        shutil.copytree(runtime.resource_root() / "deps", temporary / "deps")
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _validate_packages(target: Path) -> None:
    if target.is_symlink() or not target.is_dir():
        raise _failure("user package declaration root is unsafe")
    for name in ("apps", "deps"):
        child = target / name
        if child.is_symlink() or not child.is_dir():
            raise _failure("user package declaration tree is incomplete or unsafe")


def _activate_current(current: Path, release: Path) -> None:
    current.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if current.exists() and not current.is_symlink():
        raise _failure("active release path is not a managed link")
    temporary = current.with_name(f".{current.name}.{os.getpid()}")
    temporary.unlink(missing_ok=True)
    relative = release.relative_to(current.parent).as_posix()
    os.symlink(relative, temporary)
    os.replace(temporary, current)


def _launcher_content() -> str:
    return (
        "#!/usr/bin/env bash\n"
        f"{LAUNCHER_MARKER}\n"
        "set -euo pipefail\n"
        'if [[ -z "${HOME:-}" ]]; then\n'
        "    printf 'doubt: HOME is required\\n' >&2\n"
        "    exit 1\n"
        "fi\n"
        'data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}\n'
        'exec "$data_home/doubt/current/doubt" "$@"\n'
    )


def _managed_launcher(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise _failure("an unsafe path blocks the doubt launcher")
    content = path.read_text(encoding="utf-8")
    if LAUNCHER_MARKER not in content:
        raise _failure("an unrelated launcher exists at ~/.local/bin/doubt")
    return content == _launcher_content() and stat.S_IMODE(path.stat().st_mode) == 0o755


def _install_launcher(path: Path) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    files.text(path, _launcher_content(), 0o755, prefix=".doubt.")


def _failure(message: str) -> OperationalError:
    return OperationalError(FailureKind.UNSAFE_PATH, "activation", message)
