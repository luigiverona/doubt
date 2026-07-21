from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from doubt.core.failure import OperationalError
from doubt.system import work as work_module
from doubt.system.work import WorkRoot


class WorkRootTests(unittest.TestCase):
    def test_all_disposable_paths_share_one_private_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with WorkRoot({"TMPDIR": str(base)}) as work:
                environment = work.environment()
                self.assertEqual(work.path.stat().st_mode & 0o777, 0o700)
                for value in environment.values():
                    self.assertTrue(Path(value).is_relative_to(work.path) or value == str(work.path))
                    self.assertTrue(Path(value).exists())
                path = work.path
            self.assertFalse(path.exists())

    def test_cleanup_runs_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                with WorkRoot({"TMPDIR": str(base)}) as work:
                    path = work.path
                    (path / "tmp/residue").write_text("temporary", encoding="utf-8")
                    raise RuntimeError("injected")
            self.assertFalse(path.exists())

    def test_inherited_root_must_be_private_owned_and_absolute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir(mode=0o700)
            with WorkRoot({"DOUBT_WORK_ROOT": str(root)}) as work:
                self.assertEqual(work.path, root)
            self.assertFalse(root.exists())
        with self.assertRaises(OperationalError):
            WorkRoot({"DOUBT_WORK_ROOT": "relative"})

    def test_symlink_and_permissive_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real"
            real.mkdir(mode=0o700)
            link = base / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(OperationalError):
                WorkRoot({"DOUBT_WORK_ROOT": str(link)})
            real.chmod(0o755)
            with self.assertRaises(OperationalError):
                WorkRoot({"DOUBT_WORK_ROOT": str(real)})

    def test_term_interrupt_unwinds_and_removes_root(self):
        script = (
            "import os, signal\n"
            "from doubt.system.work import WorkRoot\n"
            "with WorkRoot() as work:\n"
            " print(work.path, flush=True)\n"
            " os.kill(os.getpid(), signal.SIGTERM)\n"
        )
        child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        path = Path(child.stdout.strip())
        self.assertEqual(child.returncode, 128 + signal.SIGTERM)
        self.assertFalse(path.exists())

    def test_repeated_close_and_externally_removed_root_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            work = WorkRoot({"TMPDIR": directory})
            work.close()
            work.close()
        with tempfile.TemporaryDirectory() as directory:
            work = WorkRoot({"TMPDIR": directory})
            shutil.rmtree(work.path)
            work.close()

    def test_non_main_thread_does_not_install_signal_handlers(self):
        failures: list[BaseException] = []
        with tempfile.TemporaryDirectory() as directory:

            def run() -> None:
                try:
                    with WorkRoot({"TMPDIR": directory}):
                        pass
                except BaseException as error:
                    failures.append(error)

            thread = threading.Thread(target=run)
            thread.start()
            thread.join()
        self.assertEqual(failures, [])

    def test_missing_roots_changed_identity_and_signal_handler_fail_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(OperationalError):
                WorkRoot({"TMPDIR": str(missing)})
            with self.assertRaises(OperationalError):
                WorkRoot({"DOUBT_WORK_ROOT": str(missing)})

            work = WorkRoot({"TMPDIR": directory})
            moved = work.path.with_name(f"{work.path.name}.moved")
            work.path.rename(moved)
            work.path.mkdir(mode=0o700)
            with self.assertRaises(OperationalError):
                work.close()

        with self.assertRaises(SystemExit) as raised:
            work_module._interrupted(signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
