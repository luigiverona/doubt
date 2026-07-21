"""Typed operational failures that may cross the application boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    BLOCKED_PRECONDITION = "blocked precondition"
    UNAVAILABLE_EXECUTABLE = "unavailable executable"
    COMMAND_FAILURE = "command failure"
    COMMAND_INTERRUPTION = "command interruption"
    MALFORMED_COMMAND_OUTPUT = "malformed command output"
    MALFORMED_PACKAGE_METADATA = "malformed package metadata"
    MALFORMED_TOML = "malformed TOML"
    MALFORMED_JSON_STATE = "malformed JSON state"
    UNSAFE_PATH = "unsafe path"
    UNSAFE_SYMLINK = "unsafe symlink"
    FILE_TYPE_MISMATCH = "file type mismatch"
    DIRECTORY_TYPE_MISMATCH = "directory type mismatch"
    PERMISSION_DENIAL = "permission denial"
    OWNERSHIP_MISMATCH = "ownership mismatch"
    ATOMIC_WRITE_FAILURE = "atomic-write failure"
    PACKAGE_METADATA_FAILURE = "package metadata failure"
    REMOTE_METADATA_UNAVAILABLE = "remote metadata unavailable"
    PACKAGE_CONFLICT_SAFETY = "package conflict safety failure"
    PACKAGE_INSTALLATION_FAILURE = "package installation failure"
    FLATPAK_FAILURE = "Flatpak failure"
    CONCURRENT_MUTATION = "concurrent mutation already running"
    INVALID_DESIRED_STATE = "invalid desired state"
    CONCURRENT_DESIRED_STATE = "concurrent desired-state change"


@dataclass(eq=False)
class OperationalError(RuntimeError):
    kind: FailureKind
    component: str
    message: str

    def __str__(self) -> str:
        return self.message
