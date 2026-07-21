"""Deterministic fault injection used at command-runner boundaries."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from doubt.core.failure import FailureKind, OperationalError
from doubt.system.run import CommandResult


@dataclass(frozen=True)
class CommandFault:
    number: int
    kind: FailureKind = FailureKind.COMMAND_FAILURE
    status: int = 1
    stdout: str = ""
    stderr: str = "injected command failure"
    interrupt: bool = False
    barrier: threading.Barrier | None = None


class FaultRunner:
    def __init__(
        self,
        home: Path,
        *,
        dry_run: bool = False,
        fault: CommandFault | None = None,
        captures: Sequence[CommandResult] = (),
        statuses: Sequence[bool] = (),
        available: set[str] | None = None,
    ) -> None:
        self.home = home
        self.dry_run = dry_run
        self.details = False
        self.fault = fault
        self.captures = list(captures)
        self.statuses = list(statuses)
        self.available = (
            available
            if available is not None
            else {
                "git",
                "gh",
                "pacman",
                "ssh",
                "ssh-keygen",
                "yay",
            }
        )
        self.attempts = 0
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str] | None] = []
        self.directories: list[Path | None] = []
        self.mutations: list[list[str]] = []
        self.notices: list[tuple[str, ...]] = []

    def home_directory(self) -> Path:
        return self.home

    def command_exists(self, command: str) -> bool:
        return command in self.available

    def succeeds(self, command: Sequence[str]) -> bool:
        self.record(command, None, None)
        return self.statuses.pop(0) if self.statuses else False

    def output(self, command: Sequence[str]) -> str:
        response = self.capture(command)
        return response.stdout if response.returncode == 0 else ""

    def capture(
        self,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.record(command, env, None)
        if self.fault_at_current():
            fault = self.fault
            assert fault is not None
            if fault.interrupt:
                raise KeyboardInterrupt
            if fault.kind is FailureKind.UNAVAILABLE_EXECUTABLE:
                return CommandResult(127, failure=fault.kind)
            return CommandResult(fault.status, fault.stdout, fault.stderr, fault.kind)
        return self.captures.pop(0) if self.captures else CommandResult(0)

    def run(
        self,
        command: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        *,
        quiet: bool = False,
    ) -> None:
        del quiet
        self.record(command, env, cwd)
        self.mutations.append(list(command))
        if not self.fault_at_current():
            return
        fault = self.fault
        assert fault is not None
        if fault.barrier is not None:
            fault.barrier.wait()
        if fault.interrupt:
            raise KeyboardInterrupt
        raise OperationalError(fault.kind, command[0], fault.stderr)

    def notice(self, *lines: str) -> None:
        self.notices.append(lines)

    def record(
        self,
        command: Sequence[str],
        env: Mapping[str, str] | None,
        cwd: Path | None,
    ) -> None:
        self.attempts += 1
        self.commands.append(list(command))
        self.environments.append(dict(env) if env is not None else None)
        self.directories.append(cwd)

    def fault_at_current(self) -> bool:
        return self.fault is not None and self.attempts == self.fault.number
