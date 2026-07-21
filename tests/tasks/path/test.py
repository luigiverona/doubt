import subprocess
import tempfile
import unittest
from pathlib import Path

from doubt.tasks import path as path_task


class FishPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_create_exact_mode_idempotent_and_verify(self):
        first = path_task.run(self.home, dry_run=False, environment={})
        target = path_task.target(self.home, {})
        self.assertEqual(target.read_text(encoding="utf-8"), path_task.CONTENT)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        second = path_task.run(self.home, dry_run=False, environment={})
        self.assertEqual(([item.status for item in first], [item.status for item in second]), (["add"], ["ok"]))
        self.assertEqual(path_task.verify(self.home, {}).status, "ok")

    def test_dry_run_and_verify_are_read_only(self):
        self.assertEqual(path_task.run(self.home, dry_run=True, environment={})[0].status, "add")
        self.assertEqual(path_task.verify(self.home, {}).status, "fail")
        self.assertFalse((self.home / ".config").exists())

    def test_xdg_root_and_unrelated_files_are_preserved(self):
        xdg = self.home / "xdg"
        fish = xdg / "fish"
        fish.mkdir(parents=True)
        unrelated = fish / "config.fish"
        unrelated.write_text("# user file\n", encoding="utf-8")
        path_task.run(self.home, dry_run=False, environment={"XDG_CONFIG_HOME": str(xdg)})
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "# user file\n")

    def test_unrelated_target_and_symlink_are_rejected(self):
        target = path_task.target(self.home, {})
        target.parent.mkdir(parents=True)
        target.write_text("# user file\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unrelated Fish"):
            path_task.run(self.home, dry_run=False, environment={})
        target.unlink()
        target.symlink_to(self.home / "elsewhere")
        with self.assertRaises(RuntimeError):
            path_task.run(self.home, dry_run=False, environment={})

    def test_fragment_avoids_duplicates_in_fresh_fish(self):
        target = path_task.target(self.home, {})
        path_task.run(self.home, dry_run=False, environment={})
        completed = subprocess.run(
            ["fish", "-c", "string join \\n $PATH"],
            capture_output=True,
            check=True,
            text=True,
            env={"HOME": str(self.home), "XDG_CONFIG_HOME": str(target.parents[2])},
        )
        output = completed.stdout.splitlines()
        expected = str(self.home / ".local" / "bin")
        self.assertEqual(output.count(expected), 1)


if __name__ == "__main__":
    unittest.main()
