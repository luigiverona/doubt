"""Interactive startup and confirmation."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from typing import TextIO, TypeVar

from ..core.failure import FailureKind, OperationalError
from ..core.task import TASK_ORDER
from ..core.version import VERSION
from .render import selected_sections

T = TypeVar("T")


def startup(
    action: Callable[[], T],
    selected_tasks: Sequence[str] = TASK_ORDER,
    stdout: TextIO | None = None,
    version: str = VERSION,
) -> T:
    output = stdout or sys.stdout
    heading(output=output, version=version)
    print("run:", file=output)
    print(", ".join(selected_sections(selected_tasks)), file=output)
    print(file=output, flush=True)
    return action()


def heading(
    stdout: TextIO | None = None,
    *,
    output: TextIO | None = None,
    version: str = VERSION,
) -> None:
    stream = output or stdout or sys.stdout
    print(f"Doubt {version}\n", file=stream)


def confirm(
    input_fn: Callable[[str], str] | None = None,
    stdout: TextIO | None = None,
) -> bool:
    output = stdout or sys.stdout
    try:
        response = (
            (input_fn("Continue? [y/N] ") if input_fn is not None else _tty_input("Continue? [y/N] ")).strip().lower()
        )
    except KeyboardInterrupt:
        print(file=output, flush=True)
        raise
    except (EOFError, OSError) as error:
        print(file=output, flush=True)
        raise OperationalError(
            FailureKind.BLOCKED_PRECONDITION,
            "confirmation",
            "an interactive controlling terminal is required before mutation",
        ) from error
    return response in {"y", "yes"}


def line(value: str = "", file: TextIO | None = None) -> None:
    print(value, file=file)


def read(message: str) -> str:
    return _tty_input(message)


def _tty_input(message: str) -> str:
    try:
        if os.environ.get("DOUBT_CONFIRM_FD") == "0":
            sys.stdout.write(message)
            sys.stdout.flush()
            value = sys.stdin.readline()
        else:
            with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as terminal:
                terminal.write(message)
                terminal.flush()
                value = terminal.readline()
    except OSError as error:
        raise OperationalError(
            FailureKind.BLOCKED_PRECONDITION,
            "terminal",
            "an interactive controlling terminal is required",
        ) from error
    if not value:
        raise EOFError
    return value.rstrip("\r\n")
