"""Verify deterministic future archive and Pages artifacts."""

from __future__ import annotations

import tempfile
from pathlib import Path

from distribution import archive, site


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="doubt-distribution-") as directory:
        root = Path(directory)
        release = root / "release"
        pages = root / "pages"
        release.mkdir()
        built, checksum = archive.deterministic(release)
        archive.inspect(built)
        site.build(pages)
        site.inspect(pages)
    print(f"distribution artifacts verified: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
