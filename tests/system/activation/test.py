from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubt.core.failure import OperationalError
from doubt.system import activation

ROOT = Path(__file__).resolve().parents[3]
DIGEST = "a" * 64


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.stage = self.root / "stage"
        self.stage.mkdir(mode=0o700)
        shutil.copytree(ROOT / "apps", self.stage / "apps")
        shutil.copytree(ROOT / "deps", self.stage / "deps")
        (self.stage / "doubt").write_text("runtime\n", encoding="utf-8")
        (self.stage / "doubt").chmod(0o755)
        (self.stage / activation.MARKER).write_text(f"version=1.0.5\nsha256={DIGEST}\n", encoding="utf-8")
        self.environment = {
            "HOME": str(self.home),
            activation.BOOTSTRAP_VARIABLE: str(self.stage),
            activation.SHA_VARIABLE: DIGEST,
        }

    def test_inspection_is_read_only_and_complete(self):
        before = tuple(self.home.rglob("*"))
        plan = activation.inspect(self.environment)
        self.assertEqual(plan.required, 3)
        self.assertTrue(plan.release and plan.declarations and plan.launcher)
        self.assertEqual(tuple(self.home.rglob("*")), before)

    def test_apply_installs_only_allowlisted_paths_and_is_idempotent(self):
        activation.apply(activation.inspect(self.environment), self.environment)
        release = self.home / ".local/share/doubt/releases/1.0.5"
        current = self.home / ".local/share/doubt/current"
        launcher = self.home / ".local/bin/doubt"
        packages = self.home / ".config/doubt/packages"
        self.assertTrue(release.is_dir())
        self.assertEqual(current.readlink(), Path("releases/1.0.5"))
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        self.assertTrue((packages / "apps/pacman/browser").is_file())
        self.assertEqual(activation.inspect(self.environment).required, 0)
        activation.apply(activation.inspect(self.environment), self.environment)
        self.assertEqual(current.readlink(), Path("releases/1.0.5"))

    def test_existing_user_declarations_are_preserved(self):
        activation.apply(activation.inspect(self.environment), self.environment)
        declaration = self.home / ".config/doubt/packages/apps/pacman/browser"
        declaration.write_text("custom-browser\n", encoding="utf-8")
        activation.apply(activation.inspect(self.environment), self.environment)
        self.assertEqual(declaration.read_text(encoding="utf-8"), "custom-browser\n")

    def test_corrupt_same_version_release_is_repaired_without_overwrite(self):
        primary = self.home / ".local/share/doubt/releases/1.0.5"
        primary.mkdir(parents=True)
        (primary / "unrelated").write_text("preserve\n", encoding="utf-8")
        activation.apply(activation.inspect(self.environment), self.environment)
        repaired = self.home / f".local/share/doubt/releases/1.0.5-{DIGEST[:12]}"
        self.assertTrue(repaired.is_dir())
        self.assertEqual((primary / "unrelated").read_text(encoding="utf-8"), "preserve\n")
        self.assertEqual(
            (self.home / ".local/share/doubt/current").readlink(),
            Path(f"releases/1.0.5-{DIGEST[:12]}"),
        )

    def test_unsafe_stage_link_is_rejected_without_persistence(self):
        (self.stage / "unsafe").symlink_to("doubt")
        with self.assertRaises(OperationalError):
            activation.inspect(self.environment)
        self.assertEqual(tuple(self.home.iterdir()), ())

    def test_invalid_environment_and_stage_metadata_are_rejected(self):
        with patch("doubt.system.activation.runtime.frozen", return_value=True):
            for environment in (
                {},
                {"HOME": "relative"},
                {"HOME": str(self.home), "XDG_DATA_HOME": "relative"},
                {
                    "HOME": str(self.home),
                    activation.BOOTSTRAP_VARIABLE: "relative",
                    activation.SHA_VARIABLE: DIGEST,
                },
                {
                    "HOME": str(self.home),
                    activation.BOOTSTRAP_VARIABLE: str(self.stage),
                    activation.SHA_VARIABLE: "invalid",
                },
            ):
                with self.subTest(environment=environment), self.assertRaises(OperationalError):
                    activation.inspect(environment)

        marker = self.stage / activation.MARKER
        marker.write_text("wrong\n", encoding="utf-8")
        with self.assertRaises(OperationalError):
            activation.inspect(self.environment)
        marker.unlink()
        with self.assertRaises(OperationalError):
            activation.inspect(self.environment)

    def test_unfrozen_checkout_needs_no_activation(self):
        with patch("doubt.system.activation.runtime.frozen", return_value=False):
            self.assertEqual(activation.inspect({}), activation.ActivationPlan(None, False, False, False))

    def test_newer_active_release_blocks_downgrade_before_mutation(self):
        data = self.home / ".local/share/doubt"
        newer = data / "releases/9.0.0"
        newer.mkdir(parents=True)
        (newer / activation.MARKER).write_text(f"version=9.0.0\nsha256={DIGEST}\n", encoding="utf-8")
        (data / "current").symlink_to("releases/9.0.0")
        before = tuple(self.home.rglob("*"))
        with self.assertRaisesRegex(OperationalError, "downgrade from Doubt 9.0.0"):
            activation.inspect(self.environment)
        self.assertEqual(tuple(self.home.rglob("*")), before)

    def test_unsupported_stage_member_is_rejected(self):
        fifo = self.stage / "unsupported"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(OperationalError, "unsupported file type"):
            activation.inspect(self.environment)

    def test_blocking_same_version_repair_path_is_rejected(self):
        releases = self.home / ".local/share/doubt/releases"
        primary = releases / "1.0.5"
        repaired = releases / f"1.0.5-{DIGEST[:12]}"
        primary.mkdir(parents=True)
        repaired.mkdir()
        with self.assertRaisesRegex(OperationalError, "blocks repair"):
            activation.apply(activation.inspect(self.environment), self.environment)

    def test_unsafe_declarations_current_and_launchers_are_rejected(self):
        packages = self.home / ".config/doubt/packages"
        packages.parent.mkdir(parents=True)
        packages.symlink_to(self.stage, target_is_directory=True)
        with self.assertRaisesRegex(OperationalError, "declaration root"):
            activation.inspect(self.environment)
        packages.unlink()
        (packages / "apps").mkdir(parents=True)
        with self.assertRaisesRegex(OperationalError, "declaration tree"):
            activation.inspect(self.environment)

        shutil.rmtree(packages)
        current = self.home / ".local/share/doubt/current"
        current.parent.mkdir(parents=True)
        current.write_text("unmanaged\n", encoding="utf-8")
        with self.assertRaisesRegex(OperationalError, "managed link"):
            activation._activate_current(current, self.stage)

        launcher = self.home / ".local/bin/doubt"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("unrelated\n", encoding="utf-8")
        with self.assertRaisesRegex(OperationalError, "unrelated launcher"):
            activation._managed_launcher(launcher)
        launcher.unlink()
        launcher.symlink_to(self.stage / "doubt")
        with self.assertRaisesRegex(OperationalError, "unsafe path"):
            activation._managed_launcher(launcher)

    def test_apply_branch_cleanup_and_existing_targets(self):
        declarations_only = activation.ActivationPlan(None, False, True, False)
        with patch(
            "doubt.system.activation.inspect",
            return_value=activation.ActivationPlan(None, False, False, False),
        ):
            activation.apply(declarations_only, {"HOME": str(self.home)})
        packages = self.home / ".config/doubt/packages"
        self.assertTrue((packages / "apps").is_dir())
        activation._materialize_defaults(packages)

        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        (left / "value").write_text("left\n", encoding="utf-8")
        (right / "value").write_text("right\n", encoding="utf-8")
        self.assertFalse(activation._trees_equal(left, right))

        releases = self.home / ".local/share/doubt/releases"
        primary = releases / "1.0.5"
        repaired = releases / f"1.0.5-{DIGEST[:12]}"
        primary.mkdir(parents=True)
        (primary / "wrong").write_text("wrong\n", encoding="utf-8")
        shutil.copytree(self.stage, repaired)
        self.assertEqual(activation._install_release(self.stage, primary, self.environment), repaired)

    def test_failed_staging_removes_temporary_trees(self):
        primary = self.home / ".local/share/doubt/releases/1.0.5"
        with patch("doubt.system.activation._validate_tree", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                activation._install_release(self.stage, primary, self.environment)
        self.assertEqual(list(primary.parent.glob(".1.0.5.stage.*")), [])

        target = self.home / ".config/doubt/packages"
        with patch("doubt.system.activation.shutil.copytree", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                activation._materialize_defaults(target)
        self.assertEqual(list(target.parent.glob(".packages.stage.*")), [])


if __name__ == "__main__":
    unittest.main()
