"""Check runtime, bootstrap, metadata, and release-tag version consistency."""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys

import doubt
from distribution.archive import ARCHIVE_NAME, expected_digest

from . import ROOT


def declared_version() -> str:
    path = ROOT / "doubt" / "core" / "version.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise RuntimeError("doubt/core/version.py has no literal VERSION assignment")


def _bootstrap_values() -> dict[str, str]:
    content = (ROOT / "bootstrap/install").read_text(encoding="utf-8")
    return dict(re.findall(r"^readonly ([A-Z0-9_]+)='([^']*)'(?:\s+#.*)?$", content, re.MULTILINE))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    args = parser.parse_args(argv)
    version = declared_version()
    errors: list[str] = []
    if doubt.__version__ != version:
        errors.append(f"runtime version {doubt.__version__} differs from {version}")
    if args.tag is not None and args.tag != version:
        errors.append(f"tag {args.tag} differs from runtime {version}")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        errors.append(f"runtime version is not canonical semantic versioning: {version}")

    values = _bootstrap_values()
    manifest = ROOT / "release/members.txt"
    expected = {
        "VERSION": version,
        "ARCHIVE_NAME": ARCHIVE_NAME,
        "ARCHIVE_ROOT": f"doubt-{version}-x86_64",
        "ARCHIVE_SHA256": expected_digest(),
        "MANIFEST_SHA256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    for name, value in expected.items():
        if values.get(name) != value:
            errors.append(f"bootstrap {name} differs from authoritative release metadata")
    expected_url = f"https://github.com/luigiverona/doubt/releases/download/{version}/{ARCHIVE_NAME}"
    if values.get("PUBLIC_ARCHIVE_URL") != expected_url:
        errors.append("bootstrap archive URL differs from the immutable exact tag")

    expected_references = {
        ROOT / "README.md": (f"self-contained {version} release", ARCHIVE_NAME),
        ROOT / "release/notes.md": (f"# doubt {version}",),
        ROOT / "site/index.html": (f"self-contained {version} release",),
        ROOT / "CHANGELOG.md": (f"## {version}",),
    }
    for path, references in expected_references.items():
        content = path.read_text(encoding="utf-8")
        for reference in references:
            if reference not in content:
                errors.append(f"{path.relative_to(ROOT)} lacks current version reference: {reference}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"version consistency passed: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
