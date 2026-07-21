"""Safe managed-path inspection."""

from __future__ import annotations

import os
from pathlib import Path

from ..core.failure import FailureKind, OperationalError


def reject(path: Path, *, directory: bool, label: str, links: bool = False) -> None:
    if path.is_symlink():
        raise OperationalError(FailureKind.UNSAFE_SYMLINK, label, f"unsafe {label}")
    if path.exists() and (not path.is_dir() if directory else not path.is_file()):
        kind = FailureKind.DIRECTORY_TYPE_MISMATCH if directory else FailureKind.FILE_TYPE_MISMATCH
        raise OperationalError(kind, label, f"unsafe {label}")
    if links and path.exists() and not directory:
        try:
            link_count = path.stat().st_nlink
        except OSError as error:
            raise OperationalError(
                FailureKind.PERMISSION_DENIAL,
                label,
                f"could not inspect {label}",
            ) from error
        if link_count != 1:
            raise OperationalError(FailureKind.UNSAFE_PATH, label, f"unsafe {label}")


def owned(path: Path, label: str) -> None:
    try:
        owner = path.stat().st_uid
    except OSError as error:
        raise OperationalError(
            FailureKind.PERMISSION_DENIAL,
            label,
            f"could not inspect owner for {label}",
        ) from error
    if owner != os.getuid():
        raise OperationalError(
            FailureKind.OWNERSHIP_MISMATCH,
            label,
            f"unsafe owner for {label}",
        )


def confined(path: Path, root: Path, label: str) -> tuple[Path, Path]:
    candidate = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(root))
    if candidate != boundary and boundary not in candidate.parents:
        raise OperationalError(FailureKind.UNSAFE_PATH, label, f"unsafe {label}")
    return candidate, boundary


def parentchain(path: Path, root: Path, label: str) -> None:
    candidate, boundary = confined(path, root, label)
    reject(boundary, directory=True, label=label)
    owned(boundary, label)
    relative = candidate.relative_to(boundary)
    current = boundary
    for part in relative.parts[:-1]:
        current /= part
        if not current.exists() and not current.is_symlink():
            break
        reject(current, directory=True, label=label)
        owned(current, label)


def ancestors(path: Path, label: str) -> None:
    """Reject existing symlinks and non-directories in a path's parent chain."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:-1]:
        current /= part
        if not current.exists() and not current.is_symlink():
            break
        reject(current, directory=True, label=label)
