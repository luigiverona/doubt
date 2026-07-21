from __future__ import annotations

import errno
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from doubt.core.failure import FailureKind, OperationalError
from doubt.system.lock import MutationLock


class MutationLockTests(unittest.TestCase):
    def test_lock_has_no_persistent_filesystem_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            before = tuple(home.iterdir())
            with MutationLock(home) as lock:
                self.assertIsNotNone(lock.socket)
            self.assertIsNone(lock.socket)
            self.assertEqual(tuple(home.iterdir()), before)

    def test_close_before_acquisition_and_repeated_close_are_safe(self):
        lock = MutationLock(Path("/unused"))
        lock.close()
        lock.close()

    def test_lock_releases_after_exception_and_interrupt(self):
        for exception in (RuntimeError("failure"), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                with self.assertRaises(type(exception)):
                    with MutationLock():
                        raise exception
                with MutationLock():
                    pass

    def test_process_contention_is_classified(self):
        script = (
            "from doubt.core.failure import OperationalError\n"
            "from doubt.system.lock import MutationLock\n"
            "try:\n"
            "  with MutationLock(): pass\n"
            "except OperationalError as error:\n"
            "  print(error.kind)\n"
            "  raise SystemExit(3)\n"
        )
        with MutationLock():
            child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(child.returncode, 3)
        self.assertEqual(child.stdout.strip(), FailureKind.CONCURRENT_MUTATION)

    def test_killed_process_releases_kernel_lock(self):
        script = (
            "from doubt.system.lock import MutationLock\n"
            "import sys\n"
            "with MutationLock():\n"
            "  print('ready', flush=True)\n"
            "  sys.stdin.read()\n"
        )
        with subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            self.assertIsNotNone(child.stdout)
            self.assertEqual(child.stdout.readline().strip(), "ready")
            child.kill()
            self.assertLess(child.wait(), 0)
        with MutationLock():
            pass

    def test_bind_failures_close_socket_and_are_classified(self):
        for number, expected in (
            (errno.EADDRINUSE, FailureKind.CONCURRENT_MUTATION),
            (errno.EPERM, FailureKind.PERMISSION_DENIAL),
        ):
            endpoint = Mock()
            endpoint.bind.side_effect = OSError(number, "injected")
            with (
                self.subTest(number=number),
                patch("doubt.system.lock.socket.socket", return_value=endpoint),
                self.assertRaises(OperationalError) as raised,
            ):
                MutationLock().__enter__()
            self.assertEqual(raised.exception.kind, expected)
            endpoint.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
