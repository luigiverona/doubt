"""Native package-domain values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeclaredPackage:
    name: str
    source: str
    path: Path


@dataclass(frozen=True)
class PackageInventory:
    native: tuple[DeclaredPackage, ...]
    flatpak: tuple[DeclaredPackage, ...]


@dataclass(frozen=True)
class Relation:
    name: str
    operator: str | None
    version: str | None
    original: str


@dataclass(frozen=True)
class PackageMetadata:
    name: str
    source: str
    version: str
    dependencies: tuple[Relation, ...]
    provides: tuple[Relation, ...]
    conflicts: tuple[Relation, ...]
    replaces: tuple[Relation, ...]


@dataclass(frozen=True)
class Conflict:
    desired: PackageMetadata
    installed: PackageMetadata
    relationship: str


@dataclass(frozen=True)
class Audit:
    inventory: PackageInventory
    desired: tuple[PackageMetadata, ...]
    installed: tuple[PackageMetadata, ...]
    conflicts: tuple[Conflict, ...]
    desired_conflict: str | None = None
