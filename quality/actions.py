"""Audit GitHub Actions references and least-privilege defaults."""

from __future__ import annotations

import re
import sys

from . import ROOT

ACTION = re.compile(r"^\s*(?:-\s+)?uses:\s+([^\s#]+)(?:\s+#\s*(v[^\s]+))?\s*$")
IMAGE = re.compile(r"^\s*image:\s+([^\s#]+)\s*$")
ARCH_DIGEST = "212b1e518e94ee9c52be55e8a32da75fcf11e7b5610b80b49479e67880102406"  # pragma: allowlist secret
ARCH_IMAGE = f"archlinux:base-devel-20260712.0.555161@sha256:{ARCH_DIGEST}"
PINNED_ACTIONS = {
    "actions/checkout": (
        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",  # pragma: allowlist secret
        "v7.0.0",
    ),
    "actions/configure-pages": (
        "45bfe0192ca1faeb007ade9deae92b16b8254a0d",  # pragma: allowlist secret
        "v6.0.0",
    ),
    "actions/deploy-pages": (
        "cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",  # pragma: allowlist secret
        "v5.0.0",
    ),
    "actions/download-artifact": (
        "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",  # pragma: allowlist secret
        "v8.0.1",
    ),
    "actions/upload-artifact": (
        "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",  # pragma: allowlist secret
        "v7.0.1",
    ),
    "actions/upload-pages-artifact": (
        "fc324d3547104276b827a68afc52ff2a11cc49c9",  # pragma: allowlist secret
        "v5.0.0",
    ),
}
LOCAL_WORKFLOWS = {"./.github/workflows/pages.yml"}
EXPECTED_WORKFLOWS = {
    "acceptance.yml",
    "ci.yml",
    "pages.yml",
    "release.yml",
    "security.yml",
}


def violations() -> list[str]:
    errors: list[str] = []
    workflows = ROOT / ".github" / "workflows"
    actual = {path.name for path in workflows.glob("*.yml")}
    if actual != EXPECTED_WORKFLOWS:
        errors.append(f"workflow set differs: expected {sorted(EXPECTED_WORKFLOWS)}, found {sorted(actual)}")
    for path in sorted(workflows.glob("*.yml")):
        content = path.read_text(encoding="utf-8")
        if "permissions:\n  contents: read" not in content:
            errors.append(f"{path.relative_to(ROOT)}: missing contents: read permission")
        for line_number, line in enumerate(content.splitlines(), 1):
            image = IMAGE.match(line)
            if image and image.group(1) != ARCH_IMAGE:
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: unpinned Arch image")
            match = ACTION.match(line)
            if not match:
                if "uses:" in line:
                    errors.append(f"{path.relative_to(ROOT)}:{line_number}: malformed action reference")
                continue
            reference, comment = match.groups()
            if reference in LOCAL_WORKFLOWS:
                if comment is not None:
                    errors.append(f"{path.relative_to(ROOT)}:{line_number}: local workflow needs no version comment")
                continue
            name, separator, revision = reference.partition("@")
            if not separator or name not in PINNED_ACTIONS:
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: unapproved action {reference}")
                continue
            expected_revision, expected_comment = PINNED_ACTIONS[name]
            if revision != expected_revision:
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: {name} is not pinned to the verified SHA")
            if comment != expected_comment:
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: {name} pin needs comment {expected_comment}")
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("GitHub Actions pinning audit failed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("GitHub Actions pinning audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
