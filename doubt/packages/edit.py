"""Validated desired-state inspection and focused package-list editing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.result import PackageCheckResult, PackageEditResult
from ..system import files
from .lists import (
    PACKAGE_FILE_MODE,
    SOURCE_ORDER,
    DesiredState,
    PackageList,
    load_lists,
    validate_category,
    validate_source,
)
from .query import validate_flatpak_id, validate_package_name

DEPENDENCY_SOURCE_ORDER = ("pacman",)
REQUIRED_BOOTSTRAP = frozenset(("base-devel", "flatpak", "git"))


def load_complete(state: DesiredState) -> tuple[list[PackageList], list[PackageList]]:
    _validate_roots(state)
    applications = load_lists(state.apps)
    dependencies = load_lists(
        state.deps,
        allowed_sources=DEPENDENCY_SOURCE_ORDER,
        require_root=True,
    )
    _validate_collections(applications, dependencies, state)
    return applications, dependencies


def listing(
    state: DesiredState,
    source: str | None = None,
    category: str | None = None,
) -> tuple[PackageList, ...]:
    applications, dependencies = load_complete(state)
    if source is not None:
        validate_source(source)
    if category is not None:
        validate_category(category)
        if source is None:
            raise _invalid("a category filter requires a package source")
    selected = [*applications, *dependencies]
    if source is not None:
        selected = [item for item in selected if item.source == source]
    if category is not None:
        matches = [item for item in selected if item.category == category]
        if not matches:
            raise _invalid(f"unknown category {category!r} for source {source}")
        selected = matches
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                SOURCE_ORDER.index(item.source),
                item.category,
                item.path.as_posix(),
            ),
        )
    )


def check(state: DesiredState) -> PackageCheckResult:
    applications, dependencies = load_complete(state)
    package_count = sum(len(item.apps) for item in (*applications, *dependencies))
    return PackageCheckResult(state.root, len(SOURCE_ORDER), package_count)


def add(
    state: DesiredState,
    source: str,
    category: str,
    package: str,
    *,
    dry_run: bool = False,
) -> PackageEditResult:
    validate_source(source)
    validate_category(category)
    _validate_package(source, package)
    applications, dependencies = load_complete(state)
    target = _target(state, dependencies, source, category)
    declarations = _declarations(applications, dependencies, package)
    if declarations:
        exact = [item for item in declarations if item.path == target]
        if exact:
            return PackageEditResult("add", source, package, state.root, category, target, dry_run, False)
        locations = ", ".join(_location(item, state) for item in declarations)
        raise _invalid(f"{package} is already declared at {locations}; packages are not moved implicitly")

    original = files.capture(target)
    content = _added_content(target, original, package)
    candidate_applications = list(applications)
    candidate_dependencies = list(dependencies)
    item = PackageList(
        source, category, tuple(sorted((*_apps_at(target, applications, dependencies), package))), target
    )
    if target.is_relative_to(state.apps):
        _replace_list(candidate_applications, item)
    else:
        _replace_list(candidate_dependencies, item)
    _validate_collections(candidate_applications, candidate_dependencies, state)
    result = PackageEditResult("add", source, package, state.root, category, target, dry_run, True)
    if dry_run:
        return result
    _persist(state, target, original, content, deleting=False)
    return result


def remove(
    state: DesiredState,
    source: str,
    package: str,
    *,
    dry_run: bool = False,
) -> PackageEditResult:
    validate_source(source)
    _validate_package(source, package)
    applications, dependencies = load_complete(state)
    matches = [item for item in (*applications, *dependencies) if item.source == source and package in item.apps]
    if not matches:
        return PackageEditResult("remove", source, package, state.root, dry_run=dry_run)
    if len(matches) != 1:
        locations = ", ".join(_location(item, state) for item in matches)
        raise _invalid(f"{package} has multiple declarations at {locations}; inspect the package lists")

    item = matches[0]
    original = files.capture(item.path)
    if original is None:
        raise _invalid(f"package-list file disappeared concurrently: {_relative(item.path, state)}")
    remaining = tuple(name for name in item.apps if name != package)
    deleting = not remaining
    if deleting and item.path.is_relative_to(state.deps):
        raise _invalid(f"{_relative(item.path, state)} is required and must not be empty")
    content = b"" if deleting else _removed_content(item.path, original, package)
    candidate_applications = list(applications)
    candidate_dependencies = list(dependencies)
    owner = candidate_applications if item.path.is_relative_to(state.apps) else candidate_dependencies
    if deleting:
        owner.remove(item)
    else:
        _replace_list(owner, replace(item, apps=remaining))
    _validate_collections(candidate_applications, candidate_dependencies, state)
    result = PackageEditResult(
        "remove",
        source,
        package,
        state.root,
        item.category,
        item.path,
        dry_run,
        True,
    )
    if dry_run:
        return result
    _persist(state, item.path, original, content, deleting=deleting)
    return result


def _validate_roots(state: DesiredState) -> None:
    for root, expected in (
        (state.apps, set(SOURCE_ORDER)),
        (state.deps, set(DEPENDENCY_SOURCE_ORDER)),
    ):
        if root.is_symlink() or not root.is_dir():
            raise _invalid(f"package-list root is missing or unsafe: {root}")
        actual = {path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()}
        if actual != expected:
            raise _invalid(f"{root}: source directories differ; expected: {', '.join(sorted(expected))}")


def _validate_collections(
    applications: Sequence[PackageList],
    dependencies: Sequence[PackageList],
    state: DesiredState,
) -> None:
    seen_packages: dict[str, list[PackageList]] = {}
    seen_categories: dict[tuple[str, str], PackageList] = {}
    for item in (*applications, *dependencies):
        boundary = state.apps if item in applications else state.deps
        resolved = item.path.resolve(strict=False)
        if resolved != boundary and boundary not in resolved.parents:
            raise _invalid(f"package-list path escapes desired-state root: {item.path}")
        _validate_package(item.source, *item.apps)
        key = (item.source, item.category)
        previous = seen_categories.get(key)
        if previous is not None and previous.path != item.path:
            raise _invalid(
                f"logical category {item.source}/{item.category} exists at "
                f"{_relative(previous.path, state)} and {_relative(item.path, state)}"
            )
        seen_categories[key] = item
        for package in item.apps:
            seen_packages.setdefault(package, []).append(item)
    duplicates = {package: locations for package, locations in seen_packages.items() if len(locations) > 1}
    if duplicates:
        details = "; ".join(
            f"{package}: {', '.join(_location(item, state) for item in locations)}"
            for package, locations in sorted(duplicates.items())
        )
        raise _invalid(f"duplicate package declarations: {details}; inspect the package lists")
    bootstrap = next(
        (item for item in dependencies if item.source == "pacman" and item.category == "bootstrap"),
        None,
    )
    if bootstrap is None:
        raise _invalid("required dependency list is missing: deps/pacman/bootstrap")
    missing = sorted(REQUIRED_BOOTSTRAP - set(bootstrap.apps))
    if missing:
        raise _invalid(f"deps/pacman/bootstrap is missing required packages: {', '.join(missing)}")


def _validate_package(source: str, *packages: str) -> None:
    for package in packages:
        if package != package.strip() or not package or any(ord(char) < 32 or ord(char) == 127 for char in package):
            raise _invalid(f"invalid package name: {package!r}")
        if source == "flatpak":
            validate_flatpak_id(package)
        else:
            validate_package_name(package)


def _target(
    state: DesiredState,
    dependencies: Sequence[PackageList],
    source: str,
    category: str,
) -> Path:
    dependency_categories = {item.category for item in dependencies}
    if category in dependency_categories:
        if source != "pacman":
            raise _invalid(f"category {category!r} is supported only for source pacman")
        return state.deps / "pacman" / category
    return state.apps / source / category


def _declarations(
    applications: Sequence[PackageList],
    dependencies: Sequence[PackageList],
    package: str,
) -> list[PackageList]:
    return [item for item in (*applications, *dependencies) if package in item.apps]


def _apps_at(
    target: Path,
    applications: Sequence[PackageList],
    dependencies: Sequence[PackageList],
) -> tuple[str, ...]:
    return next((item.apps for item in (*applications, *dependencies) if item.path == target), ())


def _replace_list(items: list[PackageList], candidate: PackageList) -> None:
    for index, item in enumerate(items):
        if item.path == candidate.path:
            items[index] = candidate
            return
    items.append(candidate)


def _added_content(path: Path, original: bytes | None, package: str) -> bytes:
    if original is None:
        return f"{package}\n".encode()
    return _canonical_lines(path, original, add=package)


def _removed_content(path: Path, original: bytes, package: str) -> bytes:
    return _canonical_lines(path, original, remove=package)


def _canonical_lines(
    path: Path,
    original: bytes,
    *,
    add: str | None = None,
    remove: str | None = None,
) -> bytes:
    if b"\r" in original:
        raise _invalid(f"{path}: package lists must use LF newlines")
    lines = original.decode("utf-8").splitlines()
    entries: list[tuple[str, str]] = []
    positions: list[int] = []
    for index, raw in enumerate(lines):
        name = raw.split("#", 1)[0].strip()
        if name:
            positions.append(index)
            entries.append((name, raw))
    if remove is not None:
        remove_index = next(index for index, (name, _) in enumerate(entries) if name == remove)
        line_index = positions.pop(remove_index)
        entries.pop(remove_index)
        lines.pop(line_index)
        positions = [index - 1 if index > line_index else index for index in positions]
    if add is not None:
        lines.append(add)
        positions.append(len(lines) - 1)
        entries.append((add, add))
    for position, (_, raw) in zip(
        positions,
        sorted(entries, key=lambda entry: entry[0]),
        strict=True,
    ):
        lines[position] = raw
    return ("\n".join(lines) + "\n").encode("utf-8")


def _persist(
    state: DesiredState,
    path: Path,
    original: bytes | None,
    content: bytes,
    *,
    deleting: bool,
) -> None:
    if deleting:
        if original is None:
            raise RuntimeError("cannot delete a package-list file without original bytes")
        files.remove_if_unchanged(path, original)
    else:
        files.replace_if_unchanged(path, original, content, PACKAGE_FILE_MODE)
    try:
        load_complete(state)
    except BaseException as error:
        try:
            if deleting:
                files.replace_if_unchanged(path, None, original or b"", PACKAGE_FILE_MODE)
            elif original is None:
                files.remove_if_unchanged(path, content)
            else:
                files.replace_if_unchanged(path, content, original, PACKAGE_FILE_MODE)
        except BaseException as rollback_error:
            raise OperationalError(
                FailureKind.ATOMIC_WRITE_FAILURE,
                "package lists",
                f"post-write validation failed and rollback could not restore {path.name}",
            ) from rollback_error
        raise error


def _location(item: PackageList, state: DesiredState) -> str:
    return f"{item.source}/{item.category} ({_relative(item.path, state)})"


def _relative(path: Path, state: DesiredState) -> str:
    return path.relative_to(state.root).as_posix()


def _invalid(message: str) -> OperationalError:
    return OperationalError(FailureKind.INVALID_DESIRED_STATE, "package lists", message)
