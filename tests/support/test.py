from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from doubt.core.failure import FailureKind, OperationalError
from doubt.system.run import CommandResult
from tests.support import CommandFault, FaultRunner


class FaultRunnerTests(unittest.TestCase):
    def test_records_commands_environments_directories_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FaultRunner(Path(directory), captures=[CommandResult(0, "out")])
            response = runner.capture(["tool", "inspect"], env={"LC_ALL": "C"})
            runner.run(["tool", "change"], cwd=Path(directory), env={"HOME": directory})
        self.assertEqual(response.stdout, "out")
        self.assertEqual(runner.commands, [["tool", "inspect"], ["tool", "change"]])
        self.assertEqual(runner.environments, [{"LC_ALL": "C"}, {"HOME": directory}])
        self.assertEqual(runner.directories, [None, Path(directory)])
        self.assertEqual(runner.mutations, [["tool", "change"]])

    def test_injects_nth_command_failure_missing_command_and_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            failure = FaultRunner(home, fault=CommandFault(2))
            failure.run(["first"])
            with self.assertRaises(OperationalError):
                failure.run(["second"])

            missing = FaultRunner(
                home,
                fault=CommandFault(1, kind=FailureKind.UNAVAILABLE_EXECUTABLE),
            )
            response = missing.capture(["missing"])
            self.assertEqual(response.returncode, 127)
            self.assertEqual(response.failure, FailureKind.UNAVAILABLE_EXECUTABLE)

            interrupted = FaultRunner(home, fault=CommandFault(1, interrupt=True))
            with self.assertRaises(KeyboardInterrupt):
                interrupted.run(["interrupted"])

    def test_barrier_synchronizes_delayed_command_without_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)
            runner = FaultRunner(
                Path(directory),
                fault=CommandFault(1, barrier=barrier),
            )
            failures = []

            def execute():
                try:
                    runner.run(["delayed"])
                except OperationalError as error:
                    failures.append(error)

            thread = threading.Thread(target=execute)
            thread.start()
            barrier.wait(timeout=2)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].kind, FailureKind.COMMAND_FAILURE)


if __name__ == "__main__":
    unittest.main()
