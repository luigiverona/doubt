from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallResult
from ..system.run import CommandRunner
from .lists import PackageList
from .model import (
    Audit,
    Conflict,
    DeclaredPackage,
    PackageInventory,
    PackageMetadata,
    Relation,
)
from .query import (
    MetadataReader,
    parse_fields,
    parse_metadata,
    parse_relation,
    relations,
    split_values,
    validate_flatpak_id,
    validate_package_name,
)

__all__ = (
    "parse_fields",
    "parse_metadata",
    "parse_relation",
    "relations",
    "split_values",
)


def build_inventory(
    app_lists: Sequence[PackageList],
    dependency_lists: Sequence[PackageList],
    selected_tasks: Iterable[str],
    complete: bool = False,
) -> PackageInventory:
    selected = set(selected_tasks)
    include_deps = complete or "deps" in selected
    include_apps = complete or "apps" in selected
    native: list[DeclaredPackage] = []
    flatpak: list[DeclaredPackage] = []
    if include_deps:
        native.extend(declared_from_lists(dependency_lists, {"pacman"}))
    elif "codex" in selected:
        native.extend(
            declared_from_lists(
                [item for item in dependency_lists if item.category == "codex"],
                {"pacman"},
            )
        )
    if include_apps:
        native.extend(declared_from_lists(app_lists, {"pacman", "aur"}))
        flatpak.extend(declared_from_lists(app_lists, {"flatpak"}))
    reject_duplicates(native, "native package")
    reject_duplicates(flatpak, "Flatpak application")
    for package in native:
        validate_package_name(package.name)
    for application in flatpak:
        validate_flatpak_id(application.name)
    return PackageInventory(tuple(native), tuple(flatpak))


def declared_from_lists(
    package_lists: Sequence[PackageList],
    sources: set[str],
) -> list[DeclaredPackage]:
    return [
        DeclaredPackage(name, package_list.source, package_list.path)
        for package_list in package_lists
        if package_list.source in sources
        for name in package_list.apps
    ]


def reject_duplicates(packages: Sequence[DeclaredPackage], label: str) -> None:
    seen: dict[str, Path] = {}
    for package in packages:
        if package.name in seen:
            raise OperationalError(
                FailureKind.PACKAGE_CONFLICT_SAFETY,
                "packages",
                f"duplicate {label} {package.name} in {seen[package.name]} and {package.path}",
            )
        seen[package.name] = package.path


def audit(
    app_lists: Sequence[PackageList],
    dependency_lists: Sequence[PackageList],
    selected_tasks: Iterable[str],
    runner: CommandRunner,
    complete: bool = False,
) -> Audit:
    inventory = build_inventory(
        app_lists,
        dependency_lists,
        selected_tasks,
        complete=complete,
    )
    reader = MetadataReader(runner)
    installed = reader.installed()
    desired = resolve_desired(inventory.native, installed, reader)
    origins = {package.name: str(package.path) for package in inventory.native}
    desired_conflict = detect_desired_conflict(desired, reader, origins)
    explicit_names = set(origins)
    conflicts = detect_installed_conflicts(desired, installed, reader, explicit_names)
    return Audit(inventory, desired, installed, conflicts, desired_conflict)


def resolve_desired(
    declared: Sequence[DeclaredPackage],
    installed: Sequence[PackageMetadata],
    reader: MetadataReader,
) -> tuple[PackageMetadata, ...]:
    resolved: list[PackageMetadata] = []
    by_name: dict[str, PackageMetadata] = {}
    installed_capabilities = capability_index(installed)
    repository_targets: list[str] = []

    for declared_package in declared:
        metadata = (
            reader.repository(declared_package.name)
            if declared_package.source == "pacman"
            else reader.aur(declared_package.name)
        )
        if metadata is None:
            raise OperationalError(
                FailureKind.PACKAGE_METADATA_FAILURE,
                "packages",
                f"selected {declared_package.source} package metadata not found: {declared_package.name}",
            )
        by_name[metadata.name] = metadata
        resolved.append(metadata)
        if declared_package.source == "pacman":
            repository_targets.append(declared_package.name)

    desired_capabilities = capability_index(resolved)
    for metadata in resolved:
        if metadata.source != "aur":
            continue
        for dependency in metadata.dependencies:
            if relation_matches_capabilities(dependency, installed_capabilities, reader):
                continue
            if relation_matches_capabilities(dependency, desired_capabilities, reader):
                continue
            repository_targets.append(dependency.original)

    transaction_names = reader.repository_transaction(repository_targets)
    for name in transaction_names:
        if name in by_name:
            continue
        metadata = reader.repository(name)
        if metadata is None:
            raise OperationalError(
                FailureKind.MALFORMED_PACKAGE_METADATA,
                "packages",
                f"repository transaction returned missing package metadata: {name}",
            )
        by_name[name] = metadata
        resolved.append(metadata)

    available = capability_index((*installed, *resolved))
    for target in repository_targets:
        relation = parse_relation(target)
        if not relation_matches_capabilities(relation, available, reader):
            raise OperationalError(
                FailureKind.MALFORMED_PACKAGE_METADATA,
                "packages",
                f"repository transaction does not satisfy requested package relation: {target}",
            )
    for package in resolved:
        for dependency in package.dependencies:
            if not relation_matches_capabilities(dependency, available, reader):
                raise OperationalError(
                    FailureKind.MALFORMED_PACKAGE_METADATA,
                    "packages",
                    f"repository transaction does not satisfy dependency {dependency.original} for {package.name}",
                )
    return tuple(resolved)


def capability_index(
    packages: Sequence[PackageMetadata],
) -> dict[str, list[tuple[str | None, PackageMetadata]]]:
    capabilities: dict[str, list[tuple[str | None, PackageMetadata]]] = {}
    for package in packages:
        capabilities.setdefault(package.name, []).append((package.version, package))
        for provided in package.provides:
            capabilities.setdefault(provided.name, []).append((provided.version, package))
    return capabilities


def relation_matches_capabilities(
    relation: Relation,
    capabilities: dict[str, list[tuple[str | None, PackageMetadata]]],
    reader: MetadataReader,
) -> bool:
    return any(version_matches(relation, version, reader) for version, _package in capabilities.get(relation.name, ()))


def version_matches(
    relation: Relation,
    candidate_version: str | None,
    reader: MetadataReader,
) -> bool:
    if relation.operator is None:
        return True
    if candidate_version is None or relation.version is None:
        return False
    comparison = reader.compare_versions(candidate_version, relation.version)
    return {
        "=": comparison == 0,
        ">": comparison > 0,
        "<": comparison < 0,
        ">=": comparison >= 0,
        "<=": comparison <= 0,
    }[relation.operator]


def detect_installed_conflicts(
    desired: Sequence[PackageMetadata],
    installed: Sequence[PackageMetadata],
    reader: MetadataReader,
    explicit_names: set[str] | None = None,
) -> tuple[Conflict, ...]:
    explicit_names = explicit_names or {package.name for package in desired}
    conflicts: list[Conflict] = []
    seen: set[tuple[str, str]] = set()
    for target in desired:
        for current in installed:
            if target.name == current.name:
                continue
            relationship = conflict_relationship(
                target,
                current,
                reader,
                desired_explicit=target.name in explicit_names,
            )
            if relationship is None:
                continue
            pair = (target.name, current.name)
            if pair not in seen:
                conflicts.append(Conflict(target, current, relationship))
                seen.add(pair)
    return tuple(conflicts)


def detect_desired_conflict(
    desired: Sequence[PackageMetadata],
    reader: MetadataReader,
    origins: dict[str, str] | None = None,
) -> str | None:
    origins = origins or {}
    for index, left in enumerate(desired):
        for right in desired[index + 1 :]:
            relationship = conflict_relationship(
                left,
                right,
                reader,
                desired_explicit=left.name in origins,
                other_explicit=right.name in origins,
            )
            if relationship is not None:
                left_origin = origins.get(left.name, "resolved dependency")
                right_origin = origins.get(right.name, "resolved dependency")
                return (
                    f"selected packages conflict: {left.name} ({left_origin}) and "
                    f"{right.name} ({right_origin}); {relationship}"
                )
    return None


def conflict_relationship(
    desired: PackageMetadata,
    other: PackageMetadata,
    reader: MetadataReader,
    *,
    desired_explicit: bool = True,
    other_explicit: bool = False,
) -> str | None:
    other_capabilities = capability_index((other,))
    desired_capabilities = capability_index((desired,))
    for relation in desired.conflicts:
        if relation_matches_capabilities(relation, other_capabilities, reader):
            return f"{desired.name} conflicts with {relation.original}"
    for relation in desired.replaces:
        if relation_matches_capabilities(relation, other_capabilities, reader):
            return f"{desired.name} replaces {relation.original}"
    for relation in other.conflicts:
        if relation_matches_capabilities(relation, desired_capabilities, reader):
            return f"{other.name} conflicts with {relation.original}"
    for relation in other.replaces:
        if relation_matches_capabilities(relation, desired_capabilities, reader):
            return f"{other.name} replaces {relation.original}"
    if desired_explicit and desired.name in other_capabilities:
        return f"{other.name} provides the explicitly selected {desired.name}"
    if other_explicit and other.name in desired_capabilities:
        return f"{desired.name} provides the explicitly selected {other.name}"
    return None


def preflight(
    app_lists: Sequence[PackageList],
    dependency_lists: Sequence[PackageList],
    selected_tasks: Iterable[str],
    runner: CommandRunner,
) -> tuple[list[InstallResult], bool]:
    try:
        audited = audit(app_lists, dependency_lists, selected_tasks, runner)
    except OperationalError as error:
        return [result(str(error), "fail")], False
    if audited.desired_conflict:
        return [result(audited.desired_conflict, "fail")], False
    if audited.conflicts:
        details = "; ".join(
            f"cannot install {item.desired.name}: it conflicts with installed "
            f"{item.installed.name}; doubt does not remove packages automatically; "
            "resolve the conflict manually and rerun"
            for item in audited.conflicts
        )
        return [result(details, "fail")], False
    return [result("conflict preflight", "ok")], True


def verify_conflicts(
    app_lists: Sequence[PackageList],
    dependency_lists: Sequence[PackageList],
    runner: CommandRunner,
) -> InstallResult:
    try:
        audited = audit(
            app_lists,
            dependency_lists,
            ("deps", "apps"),
            runner,
            complete=True,
        )
    except OperationalError as error:
        if error.kind is FailureKind.REMOTE_METADATA_UNAVAILABLE:
            return verification_result(str(error), "fail")
        return verification_result(f"package conflict verification failed: {error}", "fail")
    if audited.desired_conflict:
        return verification_result(audited.desired_conflict, "fail")
    if audited.conflicts:
        detail = "; ".join(
            f"{item.desired.name} conflicts with installed {item.installed.name}" for item in audited.conflicts
        )
        return verification_result(detail, "fail")
    return verification_result("package conflicts", "ok")


def verification_result(name: str, status: str) -> InstallResult:
    return InstallResult(name=name, source="verify", category="verify", status=status)


def result(name: str, status: str, category: str = "packages") -> InstallResult:
    return InstallResult(name=name, source="packages", category=category, status=status)
