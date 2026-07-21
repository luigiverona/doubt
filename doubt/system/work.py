"""One secure, process-owned temporary root for all disposable runtime state."""

from __future__ import annotations

import os
import shutil
import signal
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

from ..core.failure import FailureKind, OperationalError

ROOT_VARIABLE = "DOUBT_WORK_ROOT"
DIRECTORY_MODE = 0o700
SignalHandler = Callable[[int, FrameType | None], Any] | int | None


class WorkRoot:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        values = os.environ if environment is None else environment
        inherited = values.get(ROOT_VARIABLE)
        if inherited:
            path = Path(inherited)
            self.path = _validate(path)
        else:
            base = Path(values.get("TMPDIR", "/tmp"))
            if not base.is_absolute() or not base.is_dir():
                raise _failure("temporary parent must be an existing absolute directory")
            self.path = Path(tempfile.mkdtemp(prefix="doubt.", dir=base))
            os.chmod(self.path, DIRECTORY_MODE)
            self.path = _validate(self.path)

        metadata = self.path.stat(follow_symlinks=False)
        self._identity = (metadata.st_dev, metadata.st_ino)
        self._parent = os.open(
            self.path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        self._closed = False
        self._signals: dict[int, SignalHandler] = {}
        self._prepare()

    def _prepare(self) -> None:
        for name in (
            "tmp",
            "cache",
            "aur",
            "makepkg-build",
            "packages",
            "sources",
            "source-packages",
            "logs",
        ):
            path = self.path / name
            path.mkdir(mode=DIRECTORY_MODE)

    def environment(self) -> dict[str, str]:
        return {
            ROOT_VARIABLE: str(self.path),
            "TMPDIR": str(self.path / "tmp"),
            "XDG_CACHE_HOME": str(self.path / "cache"),
            "BUILDDIR": str(self.path / "makepkg-build"),
            "PKGDEST": str(self.path / "packages"),
            "SRCDEST": str(self.path / "sources"),
            "SRCPKGDEST": str(self.path / "source-packages"),
            "LOGDEST": str(self.path / "logs"),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            os.close(self._parent)
            return
        try:
            identity = (metadata.st_dev, metadata.st_ino)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or identity != self._identity
            ):
                raise _failure("temporary root changed while doubt was running; refusing unsafe cleanup")
            shutil.rmtree(self.path.name, dir_fd=self._parent)
        finally:
            os.close(self._parent)

    def __enter__(self) -> WorkRoot:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGHUP, signal.SIGTERM):
                self._signals[number] = signal.getsignal(number)
                signal.signal(number, _interrupted)
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        finally:
            for number, handler in self._signals.items():
                signal.signal(number, handler)
            self._signals.clear()


def _validate(path: Path) -> Path:
    if not path.is_absolute() or path == Path("/"):
        raise _failure("temporary root must be an absolute non-root path")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise _failure("temporary root is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != DIRECTORY_MODE
    ):
        raise _failure("temporary root must be a private user-owned directory")
    return path


def _failure(message: str) -> OperationalError:
    return OperationalError(FailureKind.UNSAFE_PATH, "temporary workspace", message)


def _interrupted(number: int, _frame: object) -> None:
    raise SystemExit(128 + number)
