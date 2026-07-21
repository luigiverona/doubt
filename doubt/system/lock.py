"""Linux kernel-backed single-instance protection without filesystem state."""

from __future__ import annotations

import errno
import os
import socket
from pathlib import Path
from types import TracebackType

from ..core.failure import FailureKind, OperationalError


class MutationLock:
    def __init__(self, _home: Path | None = None) -> None:
        self.socket: socket.socket | None = None
        self.name = f"\0doubt-mutation-{os.getuid()}"

    def __enter__(self) -> MutationLock:
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            endpoint.bind(self.name)
        except OSError as error:
            endpoint.close()
            if error.errno == errno.EADDRINUSE:
                raise OperationalError(
                    FailureKind.CONCURRENT_MUTATION,
                    "mutation",
                    "another doubt mutating run is already active",
                ) from error
            raise OperationalError(
                FailureKind.PERMISSION_DENIAL,
                "mutation lock",
                "could not acquire the doubt mutation lock",
            ) from error
        self.socket = endpoint
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        endpoint, self.socket = self.socket, None
        if endpoint is not None:
            endpoint.close()
