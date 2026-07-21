from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..system import files, runtime

SOURCE_ORDER = ("pacman", "aur", "flatpak")
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9]*$")
PACKAGE_FILE_MODE = 0o644


@dataclass(frozen=True)
class PackageList:
    source: str
    category: str
    apps: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class DesiredState:
    root: Path
    apps: Path
    deps: Path
    installed: bool


def active_state(
    *,
    runtime_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    require_materialized: bool = False,
) -> DesiredState:
    resources = (runtime_root or runtime.resource_root()).resolve(strict=True)
    release = runtime_root.resolve(strict=True) if runtime_root is not None else runtime.release_root()
    if not runtime_installed(release):
        return DesiredState(resources, resources / "apps", resources / "deps", False)

    values = os.environ if environment is None else environment
    home_value = values.get("HOME")
    if not home_value or not Path(home_value).is_absolute():
        raise _state_error("HOME must be an absolute path")
    config_value = values.get("XDG_CONFIG_HOME", str(Path(home_value) / ".config"))
    config_home = Path(config_value)
    if not config_home.is_absolute():
        raise _state_error("XDG_CONFIG_HOME must be an absolute path")
    root = config_home / "doubt" / "packages"
    if not root.exists():
        if require_materialized:
            raise _state_error("package declarations are not installed; rerun the public doubt installer")
        return DesiredState(root, resources / "apps", resources / "deps", True)
    return DesiredState(root, root / "apps", root / "deps", True)


def runtime_installed(runtime_root: Path | None = None) -> bool:
    root = runtime.release_root() if runtime_root is None else runtime_root
    return (root / ".doubt-release").is_file()


def parse_app_file(path: Path) -> tuple[str, ...]:
    apps: list[str] = []
    content = files.capture(path)
    if content is None:
        raise ValueError(f"{path}: package-list file disappeared concurrently")

    for line_number, raw_line in enumerate(content.decode("utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if any(character.isspace() for character in line):
            raise ValueError(f"{path}:{line_number}: app entries must not contain whitespace")
        apps.append(line)

    return tuple(apps)


def load_lists(
    root_dir: Path,
    *,
    allowed_sources: tuple[str, ...] = SOURCE_ORDER,
    require_root: bool = False,
) -> list[PackageList]:
    package_lists: list[PackageList] = []

    if not root_dir.exists():
        if require_root:
            raise ValueError(f"{root_dir}: package-list root is missing")
        return package_lists

    if root_dir.is_symlink() or not root_dir.is_dir():
        raise ValueError(f"{root_dir}: package-list root must be a directory")

    _validate_sources(root_dir, allowed_sources)

    for source in allowed_sources:
        source_dir = root_dir / source
        if not source_dir.exists():
            continue

        if source_dir.is_symlink() or not source_dir.is_dir():
            raise ValueError(f"{source_dir}: package source must be a directory")

        for path in sorted(source_dir.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{path}: category entries must be plain-text files")
            if path.stat().st_mode & 0o777 != PACKAGE_FILE_MODE:
                raise ValueError(f"{path}: package-list files must have mode 0644")
            category = path.name
            validate_category(category, path)
            apps = parse_app_file(path)
            if not apps:
                raise ValueError(f"{path}: package-list files must not be empty")
            package_lists.append(PackageList(source=source, category=category, apps=apps, path=path))

    return package_lists


def group_by_source(package_lists: Iterable[PackageList]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {source: [] for source in SOURCE_ORDER}

    for package_list in package_lists:
        grouped[package_list.source].extend(package_list.apps)

    return grouped


def validate_source(source: str) -> None:
    if source not in SOURCE_ORDER:
        expected = ", ".join(SOURCE_ORDER)
        raise _state_error(f"unsupported package source {source!r}; expected one of: {expected}")


def validate_category(category: str, path: Path | None = None) -> None:
    if not _CATEGORY_RE.fullmatch(category):
        location = f"{path}: " if path is not None else ""
        raise ValueError(f"{location}category names must be lowercase, one word, short, and obvious")


def _validate_sources(root_dir: Path, source_order: tuple[str, ...]) -> None:
    allowed_sources = set(source_order)
    expected = ", ".join(source_order)

    for path in sorted(root_dir.iterdir()):
        if not path.is_dir():
            raise ValueError(f"{path}: source entries must be directories named one of: {expected}")
        if path.name not in allowed_sources:
            raise ValueError(f"{path}: unknown source; expected one of: {expected}")


def _state_error(message: str) -> OperationalError:
    return OperationalError(
        FailureKind.INVALID_DESIRED_STATE,
        "package lists",
        message,
    )
