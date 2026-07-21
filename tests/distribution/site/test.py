from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from distribution import archive, site


class SiteTests(unittest.TestCase):
    def test_site_artifact_is_exact_minimal_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            site.build(first)
            site.build(second)
            self.assertEqual(
                {path.name: path.read_bytes() for path in first.iterdir()},
                {path.name: path.read_bytes() for path in second.iterdir()},
            )
            self.assertEqual((first / "install").read_bytes(), site.BOOTSTRAP.read_bytes())
            self.assertEqual({path.name for path in first.iterdir()}, {"index.html", "install"})
            self.assertNotIn("LICENSE", site.OUTPUTS)
            self.assertFalse((first / "LICENSE").exists())

    def test_landing_page_has_exact_interface_and_no_active_content(self):
        content = site.SOURCE.read_text(encoding="utf-8")
        self.assertIn(site.CANONICAL_COMMAND, content)
        for command in site.LOCAL_COMMANDS:
            self.assertIn(command, content)
        for forbidden in ("/run", "<script", "analytics", "http://"):
            self.assertNotIn(forbidden, content)
        self.assertIn("self-contained 1.0.5 release", content)
        self.assertIn("https://github.com/luigiverona/doubt", content)
        for removed in ("doubt tasks", "doubt help", "--only", "--dry-run"):
            self.assertNotIn(removed, content)

    def test_site_output_inside_repository_and_extra_files_are_rejected(self):
        with self.assertRaisesRegex(archive.ArchiveError, "outside the repository"):
            site.build(site.ROOT / "generated-site")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("extra").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(archive.ArchiveError, "must be empty"):
                site.build(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(archive.ArchiveError, "real directory"):
                site.build(link)


if __name__ == "__main__":
    unittest.main()
