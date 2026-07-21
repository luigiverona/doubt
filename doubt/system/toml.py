"""TOML parsing boundary."""

from __future__ import annotations

import tomllib
from typing import Any

from ..core.failure import FailureKind, OperationalError


def parse(content: str, label: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise OperationalError(
            FailureKind.MALFORMED_TOML,
            label,
            f"invalid {label}",
        ) from error
    if not isinstance(value, dict):
        raise OperationalError(FailureKind.MALFORMED_TOML, label, f"invalid {label}")
    return value
