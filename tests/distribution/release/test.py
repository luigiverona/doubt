from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from distribution.archive import ARCHIVE_NAME, expected_digest

ROOT = Path(__file__).resolve().parents[3]


class ReleaseTests(unittest.TestCase):
    def test_release_identity_is_exact(self):
        self.assertEqual(ARCHIVE_NAME, "doubt-1.0.5-x86_64.tar.gz")
        self.assertEqual(len(expected_digest()), 64)
        publish = (ROOT / "release/publish").read_text(encoding="utf-8")
        self.assertIn('[[ "$TAG" == "$VERSION" ]]', publish)
        self.assertIn('--title "doubt $VERSION"', publish)
        self.assertNotIn('EXPECTED_TAG="v', publish)

    def test_release_assets_are_allowlisted_and_checked(self):
        build = (ROOT / "release/build").read_text(encoding="utf-8")
        publish = (ROOT / "release/publish").read_text(encoding="utf-8")
        for name in ("COMPONENTS.json", "SHA256SUMS", "release-members.txt"):
            self.assertIn(name, build)
            self.assertIn(name, publish)
        self.assertIn("sha256sum --check --strict SHA256SUMS", publish)
        self.assertNotIn("--clobber", publish)

    def test_component_manifest_is_transparent_and_versioned(self):
        components = json.loads((ROOT / "release/components.json").read_text(encoding="utf-8"))
        self.assertEqual(components["architecture"], "x86_64")
        versions = {item["name"]: item["version"] for item in components["components"]}
        self.assertEqual(versions["doubt"], "1.0.5")
        self.assertEqual(versions["CPython"], "3.14.6")
        self.assertIn("PyInstaller bootloader", versions)

    def test_package_declarations_are_unchanged_for_the_patch_release(self):
        lines = []
        for parent in (ROOT / "apps", ROOT / "deps"):
            for path in sorted(item for item in parent.rglob("*") if item.is_file()):
                relative = path.relative_to(ROOT).as_posix()
                lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
        digest = hashlib.sha256("".join(lines).encode()).hexdigest()
        self.assertEqual(
            digest,
            "9c153faae012eeb8af18d9ca37484a8734c70d642807d828789c03a9242d8bbf",  # pragma: allowlist secret
        )

    def test_workflow_accepts_patch_tags_and_validates_the_exact_canonical_tag(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn('"1.0.*"', workflow)
        self.assertIn('[[ "$GITHUB_REF" == "refs/tags/$RELEASE_TAG" ]]', workflow)
        self.assertIn('python -m quality.version --tag "$RELEASE_TAG"', workflow)
        self.assertIn('cat-file -t "$GITHUB_REF")" = tag', workflow)
        self.assertIn('merge-base --is-ancestor', workflow)
        self.assertIn('rev-list --merges', workflow)
        self.assertNotIn('rev-list --all --count', workflow)
        self.assertIn('python3 -m quality.version --tag "$PAGE_TAG"', pages)
        self.assertIn('merge-base --is-ancestor', pages)
        self.assertNotIn('[[ "$PAGE_TAG" == "1.0.0" ]]', pages)
        self.assertNotIn('tags:\n      - "v*"', workflow)
        for forbidden in ("git push", "git commit", "git add"):
            self.assertNotIn(forbidden, workflow)

    def test_release_notes_and_changelog_preserve_one_zero_zero(self):
        notes = (ROOT / "release/notes.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(notes.startswith("# doubt 1.0.5\n"))
        self.assertEqual(changelog.count("## "), 6)
        self.assertIn("## 1.0.5", changelog)
        self.assertIn("## 1.0.1", changelog)
        self.assertIn("## 1.0.0", changelog)


if __name__ == "__main__":
    unittest.main()
