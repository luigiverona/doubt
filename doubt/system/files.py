"""Managed filesystem mutation primitives."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from . import paths


def mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise filesystem_error("inspect", path) from error


def permissions(path: Path, value: int) -> None:
    try:
        path.chmod(value)
    except OSError as error:
        raise filesystem_error("set permissions on", path) from error


def directory(
    path: Path,
    mode: int,
    *,
    parents: bool = False,
    exist_ok: bool = False,
) -> None:
    try:
        path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)
    except OSError as error:
        raise filesystem_error("create", path) from error


def remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise filesystem_error("remove", path) from error


def link(source: Path, target: Path) -> None:
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as error:
        raise OperationalError(
            FailureKind.ATOMIC_WRITE_FAILURE,
            "filesystem",
            f"failed to link managed file: {target.name}",
        ) from error


def text(path: Path, content: str, mode: int, prefix: str | None = None) -> None:
    atomic(path, content.encode("utf-8"), mode, prefix)


def atomic(
    path: Path,
    content: bytes,
    mode: int,
    prefix: str | None = None,
) -> None:
    paths.ancestors(path, "managed file path")
    paths.reject(path.parent, directory=True, label="managed file directory")
    paths.owned(path.parent, "managed file directory")
    paths.reject(path, directory=False, label="managed file", links=True)
    if path.exists():
        paths.owned(path, "managed file")
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=prefix or f".{path.name}.",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        output = os.fdopen(descriptor, "wb")
        descriptor = None
        with output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise OperationalError(
            FailureKind.ATOMIC_WRITE_FAILURE,
            "filesystem",
            f"failed to write managed file: {path.name}",
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def replace_if_unchanged(
    path: Path,
    expected: bytes | None,
    content: bytes,
    mode: int,
) -> None:
    """Atomically replace a file only while its previously read bytes still match."""
    paths.ancestors(path, "desired-state path")
    paths.reject(path.parent, directory=True, label="desired-state directory")
    paths.owned(path.parent, "desired-state directory")
    _assert_expected(path, expected)
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.doubt.",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        output = os.fdopen(descriptor, "wb")
        descriptor = None
        with output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _assert_expected(path, expected)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except OperationalError:
        raise
    except OSError as error:
        raise OperationalError(
            FailureKind.ATOMIC_WRITE_FAILURE,
            "package lists",
            f"failed to write desired-state file: {path.name}",
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def remove_if_unchanged(path: Path, expected: bytes) -> None:
    """Remove a regular file only while its previously read bytes still match."""
    paths.ancestors(path, "desired-state path")
    paths.reject(path.parent, directory=True, label="desired-state directory")
    paths.owned(path.parent, "desired-state directory")
    _assert_expected(path, expected)
    try:
        path.unlink()
        _sync_directory(path.parent)
    except OSError as error:
        raise OperationalError(
            FailureKind.ATOMIC_WRITE_FAILURE,
            "package lists",
            f"failed to remove desired-state file: {path.name}",
        ) from error


def capture(path: Path) -> bytes | None:
    """Read a desired-state file without following links, or return None if absent."""
    paths.ancestors(path, "desired-state path")
    paths.reject(path.parent, directory=True, label="desired-state directory")
    paths.owned(path.parent, "desired-state directory")
    paths.reject(path, directory=False, label="desired-state file", links=True)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        actual = None
    except OSError as error:
        raise OperationalError(
            FailureKind.UNSAFE_PATH,
            "package lists",
            f"could not safely inspect desired-state file: {path.name}",
        ) from error
    else:
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OperationalError(
                    FailureKind.UNSAFE_PATH,
                    "package lists",
                    f"unsafe desired-state file: {path.name}",
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                actual = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return actual


def _assert_expected(path: Path, expected: bytes | None) -> None:
    if capture(path) != expected:
        raise OperationalError(
            FailureKind.CONCURRENT_DESIRED_STATE,
            "package lists",
            f"desired-state file changed concurrently: {path.name}",
        )


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def lockfile(directory: Path, name: str, mode: int) -> int:
    directory_descriptor = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        return os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_descriptor,
        )
    finally:
        os.close(directory_descriptor)


def filesystem_error(action: str, path: Path) -> OperationalError:
    return OperationalError(
        FailureKind.PERMISSION_DENIAL,
        "filesystem",
        f"failed to {action} managed path: {path.name}",
    )
