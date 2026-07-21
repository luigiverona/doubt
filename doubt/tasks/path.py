"""Narrow Fish PATH integration for Doubt-owned launchers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallResult
from ..system import files, paths

MODE = 0o644
RELATIVE = Path("fish/conf.d/doubt-path.fish")
MARKER = "# Managed by Doubt: launcher PATH only"
CONTENT = f"""{MARKER}
if not contains -- $HOME/.local/bin $PATH
    set -gx PATH $HOME/.local/bin $PATH
end
"""


def target(home: Path, environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    root = Path(values.get("XDG_CONFIG_HOME", str(home / ".config")))
    if not root.is_absolute():
        raise OperationalError(FailureKind.UNSAFE_PATH, "path", "XDG_CONFIG_HOME must be absolute")
    return root / RELATIVE


def run(home: Path, *, dry_run: bool, environment: Mapping[str, str] | None = None) -> list[InstallResult]:
    path = target(home, environment)
    paths.ancestors(path, "Fish PATH integration")
    paths.reject(path, directory=False, label="Fish PATH integration", links=True)
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current is not None and current != CONTENT and MARKER not in current:
        raise OperationalError(FailureKind.UNSAFE_PATH, "path", "unrelated Fish fragment blocks PATH integration")
    changed = current != CONTENT or (path.exists() and files.mode(path) != MODE)
    if changed and not dry_run:
        files.directory(path.parent, 0o755, parents=True, exist_ok=True)
        paths.owned(path.parent, "Fish configuration directory")
        files.text(path, CONTENT, MODE, prefix=".doubt-path.")
    return [InstallResult("Fish launcher PATH", "path", "path", "add" if changed else "ok")]


def verify(home: Path, environment: Mapping[str, str] | None = None) -> InstallResult:
    path = target(home, environment)
    try:
        paths.reject(path, directory=False, label="Fish PATH integration", links=True)
        good = path.is_file() and files.mode(path) == MODE and path.read_text(encoding="utf-8") == CONTENT
    except (OSError, OperationalError, UnicodeDecodeError):
        good = False
    return InstallResult(
        "Fish launcher PATH" if good else "Fish PATH integration is missing or unsafe",
        "verify", "verify", "ok" if good else "fail",
    )
