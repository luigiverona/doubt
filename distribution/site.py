"""Build and verify the deterministic GitHub Pages artifact."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from .archive import ROOT, ArchiveError

CANONICAL_COMMAND = "curl -fsSL https://doubt.luigiverona.dev/install | bash"
LOCAL_COMMANDS = (
    "doubt",
    "doubt plan",
    "doubt verify",
    "doubt pkg list",
    "doubt pkg add SOURCE CATEGORY PACKAGE",
    "doubt pkg remove SOURCE PACKAGE",
    "doubt pkg check",
    "doubt --help",
)
SOURCE = ROOT / "site" / "index.html"
BOOTSTRAP = ROOT / "bootstrap" / "install"
OUTPUTS = {"index.html", "install"}


def _inside(candidate: Path, boundary: Path) -> bool:
    return candidate == boundary or boundary in candidate.parents


def validate_source() -> None:
    content = SOURCE.read_text(encoding="utf-8")
    if CANONICAL_COMMAND not in content:
        raise ArchiveError("site does not contain the exact canonical install command")
    for command in LOCAL_COMMANDS:
        if command not in content:
            raise ArchiveError(f"site does not contain local command: {command}")
    forbidden = ("/run", "analytics", "<script", "http://")
    for value in forbidden:
        if value in content:
            raise ArchiveError(f"site contains forbidden content: {value}")
    if not BOOTSTRAP.is_file() or BOOTSTRAP.is_symlink():
        raise ArchiveError("canonical bootstrap is missing or unsafe")


def build(destination: Path) -> None:
    validate_source()
    destination = destination.absolute()
    if _inside(destination.resolve(strict=False), ROOT.resolve(strict=True)):
        raise ArchiveError("Pages output must be built outside the repository")
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ArchiveError("Pages output path must be a real directory")
    if destination.exists() and any(destination.iterdir()):
        raise ArchiveError("Pages output directory must be empty")
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, destination / "index.html")
    shutil.copyfile(BOOTSTRAP, destination / "install")
    (destination / "index.html").chmod(0o644)
    (destination / "install").chmod(0o755)
    inspect(destination)


def inspect(destination: Path) -> None:
    actual = {path.name for path in destination.iterdir()}
    if actual != OUTPUTS:
        raise ArchiveError(f"Pages artifact differs: expected {sorted(OUTPUTS)}, found {sorted(actual)}")
    if (destination / "install").read_bytes() != BOOTSTRAP.read_bytes():
        raise ArchiveError("Pages /install differs from canonical bootstrap")
    if (destination / "index.html").read_bytes() != SOURCE.read_bytes():
        raise ArchiveError("Pages landing page differs from tracked source")
    expected_modes = {"index.html": 0o644, "install": 0o755}
    for name, expected in expected_modes.items():
        path = destination / name
        if path.is_symlink() or not path.is_file():
            raise ArchiveError(f"Pages output is not a regular file: {name}")
        if path.stat().st_mode & 0o777 != expected:
            raise ArchiveError(f"Pages output mode differs: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        build(args.destination)
    except (ArchiveError, OSError) as error:
        parser.exit(1, f"site error: {error}\n")
    print(f"Pages artifact: {os.fspath(args.destination)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
