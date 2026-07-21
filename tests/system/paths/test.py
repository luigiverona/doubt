from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubt.core.failure import FailureKind, OperationalError
from doubt.system import paths


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_path_is_safe_for_precreation_inspection(self):
        paths.reject(self.root / "missing", directory=False, label="managed file")
        paths.reject(self.root / "missing-directory", directory=True, label="managed directory")

    def test_symlink_is_rejected_for_files_and_directories(self):
        target = self.root / "target"
        target.write_text("data", encoding="utf-8")
        link = self.root / "link"
        link.symlink_to(target)
        for directory in (False, True):
            with self.subTest(directory=directory), self.assertRaisesRegex(RuntimeError, "unsafe managed"):
                paths.reject(link, directory=directory, label="managed")

    def test_file_and_directory_type_mismatches_are_rejected(self):
        file_path = self.root / "file"
        file_path.write_text("data", encoding="utf-8")
        directory_path = self.root / "directory"
        directory_path.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unsafe directory"):
            paths.reject(file_path, directory=True, label="directory")
        with self.assertRaisesRegex(RuntimeError, "unsafe file"):
            paths.reject(directory_path, directory=False, label="file")

    def test_hard_link_is_rejected_when_single_link_is_required(self):
        original = self.root / "original"
        original.write_text("data", encoding="utf-8")
        linked = self.root / "linked"
        linked.hardlink_to(original)
        with self.assertRaisesRegex(RuntimeError, "unsafe managed file"):
            paths.reject(original, directory=False, label="managed file", links=True)

    def test_owner_mismatch_is_rejected(self):
        path = self.root / "owned"
        path.write_text("data", encoding="utf-8")
        with patch("doubt.system.paths.os.getuid", return_value=path.stat().st_uid + 1):
            with self.assertRaisesRegex(RuntimeError, "unsafe owner"):
                paths.owned(path, "managed file")

    def test_stat_failure_is_classified_without_underlying_diagnostic(self):
        path = self.root / "owned"
        path.write_text("data", encoding="utf-8")
        with patch.object(Path, "stat", side_effect=OSError("private diagnostic")):
            with self.assertRaises(OperationalError) as raised:
                paths.owned(path, "managed file")
        self.assertEqual(raised.exception.kind, FailureKind.PERMISSION_DENIAL)
        self.assertNotIn("private diagnostic", str(raised.exception))

    def test_confinement_rejects_lexical_escape(self):
        candidate, boundary = paths.confined(
            self.root / "managed" / "file",
            self.root,
            "managed path",
        )
        self.assertEqual(candidate, self.root / "managed" / "file")
        self.assertEqual(boundary, self.root)
        with self.assertRaises(OperationalError) as raised:
            paths.confined(self.root.parent / "outside", self.root, "managed path")
        self.assertEqual(raised.exception.kind, FailureKind.UNSAFE_PATH)

    def test_parent_chain_rejects_symlinked_component(self):
        target = self.root / "target"
        target.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OperationalError) as raised:
            paths.parentchain(linked / "file", self.root, "managed path")
        self.assertEqual(raised.exception.kind, FailureKind.UNSAFE_SYMLINK)


if __name__ == "__main__":
    unittest.main()
