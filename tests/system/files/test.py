from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubt.core.failure import FailureKind, OperationalError
from doubt.system import files


class ManagedFileTests(unittest.TestCase):
    def test_checked_replace_and_remove_require_the_captured_file_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "browser"
            files.replace_if_unchanged(path, None, b"firefox\n", 0o644)
            self.assertEqual(path.read_bytes(), b"firefox\n")
            self.assertEqual(files.mode(path), 0o644)
            self.assertEqual(list(root.glob(".browser.doubt.*")), [])

            with self.assertRaises(OperationalError) as raised:
                files.replace_if_unchanged(path, b"stale\n", b"replacement\n", 0o644)
            self.assertEqual(raised.exception.kind, FailureKind.CONCURRENT_DESIRED_STATE)
            self.assertEqual(path.read_bytes(), b"firefox\n")

            with self.assertRaises(OperationalError):
                files.remove_if_unchanged(path, b"stale\n")
            files.remove_if_unchanged(path, b"firefox\n")
            self.assertFalse(path.exists())

    def test_atomic_write_sets_mode_newline_and_replaces_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed"
            path.write_text("old\n", encoding="utf-8")
            files.text(path, "new\n", 0o600)
            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(files.mode(path), 0o600)
            self.assertEqual(list(path.parent.glob(".managed.*")), [])

    def test_replace_failure_preserves_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed"
            path.write_text("original\n", encoding="utf-8")
            with patch("doubt.system.files.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OperationalError) as raised:
                    files.text(path, "replacement\n", 0o600)
            self.assertEqual(raised.exception.kind, FailureKind.ATOMIC_WRITE_FAILURE)
            self.assertEqual(str(raised.exception), "failed to write managed file: managed")
            self.assertEqual(path.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(list(path.parent.glob(".managed.*")), [])

    def test_data_fsync_failure_preserves_original_without_leaking_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed"
            path.write_text("original\n", encoding="utf-8")
            with patch("doubt.system.files.os.fsync", side_effect=OSError("sensitive-value")):
                with self.assertRaises(OperationalError) as raised:
                    files.text(path, "private-content\n", 0o600)
            self.assertEqual(path.read_text(encoding="utf-8"), "original\n")
            self.assertNotIn("private-content", str(raised.exception))
            self.assertNotIn("sensitive-value", str(raised.exception))

    def test_pre_stream_failure_closes_temporary_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed"
            original_close = os.close
            closed = []

            def record_close(descriptor):
                closed.append(descriptor)
                original_close(descriptor)

            with (
                patch("doubt.system.files.os.fchmod", side_effect=OSError("mode failure")),
                patch("doubt.system.files.os.close", side_effect=record_close),
            ):
                with self.assertRaises(OperationalError):
                    files.text(path, "content\n", 0o600)
            self.assertEqual(len(closed), 1)
            self.assertEqual(list(path.parent.glob(".managed.*")), [])

    def test_temporary_cleanup_failure_does_not_mask_atomic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed"
            path.write_text("original\n", encoding="utf-8")
            original_unlink = Path.unlink

            def fail_temporary_unlink(candidate, *args, **kwargs):
                if candidate.name.startswith(".managed."):
                    raise OSError("cleanup failed")
                return original_unlink(candidate, *args, **kwargs)

            with (
                patch("doubt.system.files.os.replace", side_effect=OSError("replace failed")),
                patch("doubt.system.files.Path.unlink", new=fail_temporary_unlink),
            ):
                with self.assertRaises(OperationalError) as raised:
                    files.text(path, "replacement\n", 0o600)
            self.assertEqual(raised.exception.kind, FailureKind.ATOMIC_WRITE_FAILURE)
            self.assertEqual(path.read_text(encoding="utf-8"), "original\n")

    def test_symlink_target_and_symlinked_parent_component_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("original", encoding="utf-8")
            link = root / "managed"
            link.symlink_to(target)
            with self.assertRaises(OperationalError) as raised:
                files.text(link, "replacement", 0o600)
            self.assertEqual(raised.exception.kind, FailureKind.UNSAFE_SYMLINK)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

            managed = root / "managed-root"
            managed.mkdir()
            linked = managed / "linked"
            linked.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OperationalError) as raised:
                files.text(linked / "file", "replacement", 0o600)
            self.assertEqual(raised.exception.kind, FailureKind.UNSAFE_SYMLINK)

    def test_special_file_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed"
            os.mkfifo(path)
            with self.assertRaises(OperationalError) as raised:
                files.text(path, "replacement", 0o600)
            self.assertEqual(raised.exception.kind, FailureKind.FILE_TYPE_MISMATCH)

    def test_mutation_primitives_classify_permission_failures_without_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_text("value", encoding="utf-8")
            cases = (
                ("Path.stat", lambda: files.mode(source)),
                ("Path.chmod", lambda: files.permissions(source, 0o600)),
                ("Path.mkdir", lambda: files.directory(root / "new", 0o700)),
                ("Path.unlink", lambda: files.remove(source)),
            )
            for target, operation in cases:
                with (
                    self.subTest(target=target),
                    patch(
                        f"doubt.system.files.{target}",
                        side_effect=OSError("private diagnostic"),
                    ),
                ):
                    with self.assertRaises(OperationalError) as raised:
                        operation()
                self.assertEqual(raised.exception.kind, FailureKind.PERMISSION_DENIAL)
                self.assertNotIn("private diagnostic", str(raised.exception))

            with patch("doubt.system.files.os.link", side_effect=OSError("private diagnostic")):
                with self.assertRaises(OperationalError) as raised:
                    files.link(source, root / "target")
            self.assertEqual(raised.exception.kind, FailureKind.ATOMIC_WRITE_FAILURE)
            self.assertNotIn("private diagnostic", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
