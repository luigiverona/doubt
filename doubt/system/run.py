from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from ..core.failure import FailureKind, OperationalError
from .env import merged

SENSITIVE_FIELDS = {
    "authorization",
    "client_secret",
    "credential",
    "key",
    "password",
    "secret",
    "token",
}
DIAGNOSTIC_BYTES = 8192
DIAGNOSTIC_CHARACTERS = 2000
DIAGNOSTIC_LINES = 8
PROVIDER_DETAIL_LINES = 200
PROVIDER_LINE_CHARACTERS = 1000
TERMINATION_TIMEOUT_SECONDS = 2
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
ACTIONABLE_PROVIDER_LINE = re.compile(r"\b(?:error|warning):", re.IGNORECASE)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:authorization|access[_-]?token|auth[_-]?token|client[_-]?secret|credential|"
    r"id[_-]?token|password|private[_-]?key|refresh[_-]?token|secret|token)\b"
    r"[\"']?\s*[:=]\s*(?:bearer\s+)?)([^\s,;&\"']+)"
)
SENSITIVE_QUERY = re.compile(r"(?i)([?&](?:code|device_code|token|access_token|refresh_token)=)[^&\s]+")
BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
TOKEN_VALUE = re.compile(r"\b(?:gh[oprsu]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b")


class InputPolicy(StrEnum):
    CLOSED = "closed"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    failure: FailureKind | None = None


class CommandRunner:
    def __init__(
        self,
        dry_run: bool = False,
        details: bool = False,
        writer: Callable[[str], None] = print,
        environment: Mapping[str, str] | None = None,
        terminal: bool = False,
        available_commands: Sequence[str] = (),
    ) -> None:
        self.dry_run = dry_run
        self.details = details
        self.writer = writer
        self.environment = dict(environment or {})
        self.terminal = terminal
        self.available_commands = set(available_commands)

    def command_exists(self, command: str) -> bool:
        return command in self.available_commands or shutil.which(command) is not None

    def home_directory(self) -> Path:
        return Path.home()

    def succeeds(self, command: Sequence[str]) -> bool:
        validate_command(command)
        self._echo("inspect", command)
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                env=merged(self.environment),
            )
        except FileNotFoundError:
            return False
        return result.returncode == 0

    def output(self, command: Sequence[str]) -> str:
        validate_command(command)
        self._echo("inspect", command)
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                env=merged(self.environment),
            )
        except FileNotFoundError:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout

    def capture(
        self,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        validate_command(command)
        self._echo("inspect", command)
        command_env = merged(self.environment, env)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                env=command_env,
            )
        except FileNotFoundError:
            return CommandResult(127, failure=FailureKind.UNAVAILABLE_EXECUTABLE)
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def run(
        self,
        command: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        *,
        quiet: bool = False,
        input_policy: InputPolicy = InputPolicy.CLOSED,
    ) -> None:
        validate_command(command)
        command_text = command_text_for(command)
        if cwd is not None:
            command_text = f"cd {shlex.quote(str(cwd))} && {command_text}"

        if self.dry_run:
            self._echo_text("planned", command_text)
            return

        self._echo_text("run", command_text)
        try:
            command_env = merged(self.environment, env)
            assert command_env is not None
            if input_policy is InputPolicy.TERMINAL and self.terminal:
                if os.environ.get("DOUBT_CONFIRM_FD") == "0":
                    self._invoke(command, cwd, command_env, sys.stdin, quiet)
                else:
                    with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as terminal:
                        self._invoke(command, cwd, command_env, terminal, quiet)
            else:
                self._invoke(command, cwd, command_env, subprocess.DEVNULL, quiet)
        except FileNotFoundError as error:
            raise OperationalError(
                FailureKind.UNAVAILABLE_EXECUTABLE,
                command[0] if command else "command",
                f"failed to run command: {command_text}",
            ) from error
        except OSError as error:
            raise OperationalError(
                FailureKind.BLOCKED_PRECONDITION,
                "terminal",
                "an interactive controlling terminal is required for external operations",
            ) from error
        except subprocess.CalledProcessError as error:
            kind = FailureKind.COMMAND_INTERRUPTION if error.returncode < 0 else FailureKind.COMMAND_FAILURE
            message = f"command failed with exit code {error.returncode}: {command_text}"
            if error.output is not None:
                diagnostic = _diagnostic_text(error.output)
                if diagnostic:
                    message += f"\nProvider output:\n{diagnostic}"
                message += "\nRerun with `doubt --verbose` for complete provider output."
            raise OperationalError(
                kind,
                command[0] if command else "command",
                message,
            ) from error

    def _invoke(
        self,
        command: Sequence[str],
        cwd: Path | None,
        command_env: Mapping[str, str],
        stdin: int | IO[Any] | None,
        quiet: bool,
    ) -> None:
        if quiet:
            self._quiet_run(command, cwd, command_env, stdin)
            return
        subprocess.run(command, cwd=cwd, check=True, env=command_env, stdin=stdin)

    def _quiet_run(
        self,
        command: Sequence[str],
        cwd: Path | None,
        command_env: Mapping[str, str],
        stdin: int | IO[Any] | None,
    ) -> None:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=command_env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
        )
        tail = bytearray()
        pending = bytearray()
        emitted = 0
        limit = PROVIDER_DETAIL_LINES if self.details else DIAGNOSTIC_LINES
        try:
            if process.stdout is None:
                raise RuntimeError("provider output pipe is unavailable")
            with process.stdout:
                while block := process.stdout.read(8192):
                    tail.extend(block[-DIAGNOSTIC_BYTES:])
                    if len(tail) > DIAGNOSTIC_BYTES:
                        del tail[:-DIAGNOSTIC_BYTES]
                    pending.extend(block)
                    emitted += self._emit_provider_lines(pending, limit - emitted)
                    if len(pending) > DIAGNOSTIC_BYTES:
                        del pending[:-DIAGNOSTIC_BYTES]
                emitted += self._emit_provider_lines(pending, limit - emitted, final=True)
            returncode = process.wait()
        except BaseException:
            _terminate_and_reap(process)
            raise
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command, output=bytes(tail))

    def _emit_provider_lines(self, pending: bytearray, remaining: int, *, final: bool = False) -> int:
        emitted = 0
        while pending:
            newline = pending.find(b"\n")
            carriage = pending.find(b"\r")
            endings = [position for position in (newline, carriage) if position >= 0]
            if not endings and not final:
                return emitted
            ending = min(endings) if endings else len(pending)
            raw = bytes(pending[:ending])
            del pending[: ending + (1 if endings else 0)]
            line = _diagnostic_line(raw)
            visible = self.details or (line and ACTIONABLE_PROVIDER_LINE.search(line))
            if line and visible and emitted < remaining:
                self.writer(line)
                emitted += 1
        return emitted

    def _echo(self, prefix: str, command: Sequence[str]) -> None:
        self._echo_text(prefix, command_text_for(command))

    def _echo_text(self, prefix: str, command: str) -> None:
        if self.details:
            self.writer(f"{prefix}: {command}")

    def notice(self, *lines: str) -> None:
        for line in lines:
            self.writer(line)


def command_text_for(command: Sequence[str]) -> str:
    redacted: list[str] = []
    hide_next = False
    for argument in command:
        if hide_next:
            redacted.append("[redacted]")
            hide_next = False
            continue
        if argument.startswith("--") and sensitive_name(argument.lstrip("-")):
            redacted.append(argument)
            hide_next = True
            continue
        field, separator, _value = argument.partition("=")
        if separator and sensitive_name(field.lstrip("-")):
            redacted.append(f"{field}=[redacted]")
        else:
            redacted.append(argument)
    return shlex.join(redacted)


def validate_command(command: Sequence[str]) -> None:
    if not command or not isinstance(command[0], str) or not command[0]:
        raise ValueError("external command must not be empty")
    for argument in command:
        if not isinstance(argument, str) or "\0" in argument:
            raise ValueError("external command contains an invalid argument")


def sensitive_name(value: str) -> bool:
    return value.lower().translate({ord("-"): ord("_")}) in SENSITIVE_FIELDS


def _redact_text(value: str) -> str:
    value = SENSITIVE_ASSIGNMENT.sub(r"\1[redacted]", value)
    value = SENSITIVE_QUERY.sub(r"\1[redacted]", value)
    value = BEARER_VALUE.sub(r"\1[redacted]", value)
    return TOKEN_VALUE.sub("[redacted]", value)


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            _signal_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.poll()


def _signal_process_group(process: subprocess.Popen[bytes], requested: signal.Signals) -> None:
    try:
        os.killpg(process.pid, requested)
    except ProcessLookupError:
        pass


def _diagnostic_line(output: bytes) -> str:
    decoded = output.decode("utf-8", errors="replace")
    return _redact_text(ANSI_ESCAPE.sub("", decoded)).strip()[-PROVIDER_LINE_CHARACTERS:]


def _diagnostic_text(output: object) -> str:
    if not isinstance(output, (bytes, str)) or not output:
        return ""
    decoded = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    clean = _redact_text(ANSI_ESCAPE.sub("", decoded))
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    diagnostic = "\n".join(lines[-DIAGNOSTIC_LINES:])
    return diagnostic[-DIAGNOSTIC_CHARACTERS:]
