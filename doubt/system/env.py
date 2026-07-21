"""Controlled command environments."""

from __future__ import annotations

from collections.abc import Mapping
from os import environ


def current() -> Mapping[str, str]:
    return environ


def merged(
    base: Mapping[str, str] | None,
    values: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    if base is None and values is None:
        return None
    return {**environ, **(base or {}), **(values or {})}
