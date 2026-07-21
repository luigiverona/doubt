from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from distribution import archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / archive.ARCHIVE_ROOT
        (self.bundle / "_internal/apps").mkdir(parents=True)
        (self.bundle / "licenses").mkdir()
        (self.bundle / "doubt").write_bytes(b"executable\n")
        (self.bundle / "doubt").chmod(0o755)
        (self.bundle / "LICENSE").write_text("license\n", encoding="utf-8")
        (self.bundle / "COMPONENTS.json").write_text(json.dumps({"architecture": "x86_64"}), encoding="utf-8")
        (self.bundle / "licenses/test.txt").write_text("license\n", encoding="utf-8")
        (self.bundle / "_internal/apps/list").write_text("package\n", encoding="utf-8")

    def build(self, name: str = "archive.tar.gz") -> Path:
        output = self.root / name
        expected = archive.manifest(self.bundle)
        with patch("distribution.archive.expected_manifest", return_value=expected):
            archive.build(self.bundle, output)
            archive.inspect(output)
        return output

    def raw_archive(self, member: tarfile.TarInfo, content: bytes = b"") -> Path:
        output = self.root / "unsafe.tar.gz"
        with output.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as built:
                    member.size = len(content)
                    built.addfile(member, io.BytesIO(content) if content else None)
        return output

    def test_archive_is_byte_identical_and_metadata_is_normalized(self):
        first = self.build("first.tar.gz")
        second = self.build("second.tar.gz")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first, "r:gz") as built:
            for member in built.getmembers():
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                expected = 0o755 if member.isdir() or member.name.endswith("/doubt") else 0o644
                self.assertEqual(member.mode, expected)

    def test_archive_has_one_normalized_root_and_exact_order(self):
        built = self.build()
        with tarfile.open(built, "r:gz") as opened:
            actual = tuple(f"{'d' if member.isdir() else 'f'} {member.name}" for member in opened.getmembers())
        self.assertEqual(actual, archive.manifest(self.bundle))
        self.assertTrue(all(name.split(" ", 1)[1].startswith(archive.ARCHIVE_ROOT) for name in actual))

    def test_traversal_absolute_and_non_normalized_members_are_rejected(self):
        for name in ("../escape", "/absolute", "root//file", "root/./file", "root\\file"):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                with self.assertRaises(archive.ArchiveError):
                    archive.inspect(self.raw_archive(member, b"x"))

    def test_symbolic_hard_and_special_members_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo(f"{archive.ARCHIVE_ROOT}/unsafe")
                member.type = kind
                member.mode = 0o644
                with self.assertRaises(archive.ArchiveError):
                    archive.inspect(self.raw_archive(member))

    def test_setuid_and_non_normalized_metadata_are_rejected(self):
        member = tarfile.TarInfo(f"{archive.ARCHIVE_ROOT}/unsafe")
        member.mode = 0o4644
        member.uid = os.getuid()
        with self.assertRaises(archive.ArchiveError):
            archive.inspect(self.raw_archive(member, b"x"))

    def test_runtime_symlink_is_rejected_before_archive_creation(self):
        (self.bundle / "LICENSE").unlink()
        (self.bundle / "LICENSE").symlink_to("COMPONENTS.json")
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            archive.build(self.bundle, self.root / "output.tar.gz")

    def test_digest_is_stable_and_expected_digest_is_exact(self):
        built = self.build()
        self.assertEqual(len(archive.digest(built)), 64)
        self.assertEqual(
            archive.expected_digest(),
            "a75645377468c6fb01930f7294b88af3d6d3c0e7146f3c99f8c51d38a679d7ce",  # pragma: allowlist secret
        )

    def test_committed_member_manifest_is_unique_bounded_and_runtime_only(self):
        members = archive.expected_manifest()
        self.assertEqual(len(members), len(set(members)))
        self.assertLessEqual(len(members), archive.MAX_MEMBERS)
        self.assertIn(f"f {archive.ARCHIVE_ROOT}/doubt", members)
        for forbidden in (".git", "tests/", "requirements/", "bootstrap/install"):
            self.assertFalse(any(forbidden in member for member in members))


if __name__ == "__main__":
    unittest.main()
