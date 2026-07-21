from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallItem:
    name: str
    source: str
    category: str


@dataclass(frozen=True)
class InstallResult:
    name: str
    source: str
    category: str
    status: str


@dataclass(frozen=True)
class PackageEditResult:
    action: str
    source: str
    package: str
    root: Path
    category: str | None = None
    path: Path | None = None
    dry_run: bool = False
    changed: bool = False


@dataclass(frozen=True)
class PackageCheckResult:
    root: Path
    sources: int
    packages: int
