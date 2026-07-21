from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

from doubt.core.failure import FailureKind, OperationalError
from doubt.system.run import (
    TERMINATION_TIMEOUT_SECONDS,
    CommandResult,
    CommandRunner,
    InputPolicy,
    _terminate_and_reap,
    command_text_for,
)


class CommandRunnerTests(unittest.TestCase):
    def test_command_and_home_inspection_delegate_without_mutation(self):
        runner = CommandRunner()
        with patch("doubt.system.run.shutil.which", return_value="/usr/bin/tool") as which:
            self.assertTrue(runner.command_exists("tool"))
        which.assert_called_once_with("tool")
        with patch("doubt.system.run.Path.home", return_value=Path("/safe/home")):
            self.assertEqual(runner.home_directory(), Path("/safe/home"))

    def test_succeeds_handles_status_and_missing_executable(self):
        runner = CommandRunner()
        for returncode, expected in ((0, True), (1, False)):
            with (
                self.subTest(returncode=returncode),
                patch(
                    "doubt.system.run.subprocess.run",
                    return_value=subprocess.CompletedProcess(["tool"], returncode),
                ),
            ):
                self.assertEqual(runner.succeeds(["tool"]), expected)
        with patch("doubt.system.run.subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(runner.succeeds(["missing"]))

    def test_output_handles_success_failure_and_missing_executable(self):
        runner = CommandRunner()
        cases = ((0, "value\n", "value\n"), (2, "ignored", ""))
        for returncode, stdout, expected in cases:
            with (
                self.subTest(returncode=returncode),
                patch(
                    "doubt.system.run.subprocess.run",
                    return_value=subprocess.CompletedProcess(["tool"], returncode, stdout),
                ),
            ):
                self.assertEqual(runner.output(["tool"]), expected)
        with patch("doubt.system.run.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(runner.output(["missing"]), "")

    def test_capture_returns_deterministic_result_and_missing_status(self):
        runner = CommandRunner()
        completed = subprocess.CompletedProcess(["tool"], 3, "out", "err")
        with patch("doubt.system.run.subprocess.run", return_value=completed) as run:
            self.assertEqual(
                runner.capture(["tool"], env={"LC_ALL": "C"}),
                CommandResult(3, "out", "err"),
            )
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        with patch("doubt.system.run.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(
                runner.capture(["missing"]),
                CommandResult(127, failure=FailureKind.UNAVAILABLE_EXECUTABLE),
            )

    def test_run_uses_argument_array_and_explicit_working_directory(self):
        runner = CommandRunner()
        with patch("doubt.system.run.subprocess.run") as run:
            runner.run(["tool", "argument"], cwd=Path("/tmp"), env={"LC_ALL": "C"})
        self.assertEqual(run.call_args.args[0], ["tool", "argument"])
        self.assertEqual(run.call_args.kwargs["cwd"], Path("/tmp"))
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_dry_run_terminal_and_tty_failure_paths(self):
        output = StringIO()
        runner = CommandRunner(dry_run=True, details=True, writer=lambda line: print(line, file=output))
        with patch("doubt.system.run.subprocess.run") as run:
            runner.run(["tool"], input_policy=InputPolicy.TERMINAL)
        run.assert_not_called()
        self.assertEqual(output.getvalue(), "planned: tool\n")

        runner = CommandRunner(terminal=True)
        with (
            patch.dict("os.environ", {"DOUBT_CONFIRM_FD": "0"}),
            patch("doubt.system.run.subprocess.run") as run,
        ):
            runner.run(["tool"], input_policy=InputPolicy.TERMINAL)
        self.assertIs(run.call_args.kwargs["stdin"], sys.stdin)

        terminal = StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("builtins.open", return_value=terminal) as opened,
            patch("doubt.system.run.subprocess.run") as run,
        ):
            runner.run(["tool"], input_policy=InputPolicy.TERMINAL)
        opened.assert_called_once_with("/dev/tty", "r+", encoding="utf-8", buffering=1)
        self.assertIs(run.call_args.kwargs["stdin"], terminal)

        with patch("builtins.open", side_effect=OSError("no tty")):
            with self.assertRaises(OperationalError) as raised:
                runner.run(["tool"], input_policy=InputPolicy.TERMINAL)
        self.assertEqual(raised.exception.kind, FailureKind.BLOCKED_PRECONDITION)

    def test_run_reports_missing_and_failed_commands(self):
        runner = CommandRunner()
        with patch("doubt.system.run.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "failed to run command: missing"):
                runner.run(["missing"])
        failure = subprocess.CalledProcessError(7, ["tool"])
        with patch("doubt.system.run.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "command failed with exit code 7: tool"):
                runner.run(["tool"])

    def test_normal_mode_suppresses_flatpak_chatter_and_verbose_streams_it(self):
        chatter = (
            "Looking for matches?\n"
            "example.Application permissions:\n"
            "ID Branch Op Remote Download\n"
            "Installation complete.\n"
        )
        child = f"import sys; sys.stdout.write({chatter!r}); sys.stdout.flush()"
        base = "from doubt.system.run import CommandRunner; import sys; "
        normal = base + f"CommandRunner().run([sys.executable, '-c', {child!r}], quiet=True)"
        verbose = base + f"CommandRunner(details=True).run([sys.executable, '-c', {child!r}], quiet=True)"

        hidden = subprocess.run([sys.executable, "-c", normal], capture_output=True, text=True, check=False)
        shown = subprocess.run([sys.executable, "-c", verbose], capture_output=True, text=True, check=False)

        self.assertEqual((hidden.returncode, hidden.stdout, hidden.stderr), (0, "", ""))
        self.assertEqual(shown.returncode, 0)
        for line in chatter.splitlines():
            self.assertIn(line, shown.stdout)

    def test_normal_mode_keeps_actionable_provider_warnings(self):
        child = "print('routine progress'); print('warning: provider key expires soon')"
        script = (
            "from doubt.system.run import CommandRunner; import sys; "
            f"CommandRunner().run([sys.executable, '-c', {child!r}], quiet=True)"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "warning: provider key expires soon\n")

    def test_provider_output_is_redacted_and_bounded_in_both_modes(self):
        secret = "github_" + "pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # pragma: allowlist secret
        lines = [f"warning: token={secret}-{number}" for number in range(240)]
        child = "import os; print(os.environ['PROVIDER_LINES'])"
        base = "from doubt.system.run import CommandRunner; import sys; "
        environment = {**os.environ, "PROVIDER_LINES": "\n".join(lines)}
        normal = base + f"CommandRunner().run([sys.executable, '-c', {child!r}], quiet=True)"
        verbose = base + f"CommandRunner(details=True).run([sys.executable, '-c', {child!r}], quiet=True)"

        hidden = subprocess.run(
            [sys.executable, "-c", normal], capture_output=True, text=True, check=False, env=environment
        )
        shown = subprocess.run(
            [sys.executable, "-c", verbose], capture_output=True, text=True, check=False, env=environment
        )

        self.assertEqual(hidden.returncode, 0)
        self.assertEqual(shown.returncode, 0)
        self.assertEqual(len(hidden.stdout.splitlines()), 8)
        self.assertEqual(len(shown.stdout.splitlines()), 201)  # command plus bounded provider details
        self.assertLessEqual(len(shown.stdout), 202_000)
        self.assertNotIn(secret, hidden.stdout)
        self.assertNotIn(secret, shown.stdout)
        self.assertIn("token=[redacted]", hidden.stdout)

    def test_failed_provider_diagnostics_redact_headers_urls_and_tokens(self):
        secret = "gho_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # pragma: allowlist secret
        script = "import os, sys; print(os.environ['PROVIDER_FAILURE']); sys.exit(7)"
        output = (
            f"Authorization: Bearer {secret}\n"
            f"error: https://example.test/login?code={secret}&device_code={secret}"
        )
        notices: list[str] = []
        with self.assertRaises(OperationalError) as raised:
            CommandRunner(environment={"PROVIDER_FAILURE": output}, writer=notices.append).run(
                [sys.executable, "-c", script], quiet=True
            )
        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertIn("Authorization: Bearer [redacted]", message)
        self.assertIn("code=[redacted]&device_code=[redacted]", message)

    def test_normal_provider_failure_is_nonzero_actionable_and_bounded(self):
        runner = CommandRunner()
        script = "import sys; print('discard-' + 'x' * 12000); print('flatpak network unavailable'); sys.exit(7)"
        with self.assertRaises(OperationalError) as raised:
            runner.run([sys.executable, "-c", script], quiet=True)
        message = str(raised.exception)
        self.assertEqual(raised.exception.kind, FailureKind.COMMAND_FAILURE)
        self.assertIn("exit code 7", message)
        self.assertIn("flatpak network unavailable", message)
        self.assertIn("doubt --verbose", message)
        self.assertLessEqual(len(message), 2400)

    def test_package_provider_closes_stdin_and_authentication_stays_streamed(self):
        runner = CommandRunner(terminal=True)
        with (
            patch.dict("os.environ", {"DOUBT_CONFIRM_FD": "0"}),
            patch.object(runner, "_quiet_run") as quiet,
        ):
            runner.run(["sudo", "pacman", "-S", "package"], quiet=True)
        self.assertEqual(quiet.call_args.args[3], subprocess.DEVNULL)

        with (
            patch.dict("os.environ", {"DOUBT_CONFIRM_FD": "0"}),
            patch("doubt.system.run.subprocess.run") as run,
        ):
            runner.run(["gh", "auth", "login"], input_policy=InputPolicy.TERMINAL)
        self.assertIs(run.call_args.kwargs["stdin"], sys.stdin)
        self.assertNotIn("stdout", run.call_args.kwargs)
        self.assertNotIn("stderr", run.call_args.kwargs)

    def test_run_classifies_operational_failures(self):
        runner = CommandRunner()
        cases = (
            (FileNotFoundError(), FailureKind.UNAVAILABLE_EXECUTABLE),
            (subprocess.CalledProcessError(7, ["tool"]), FailureKind.COMMAND_FAILURE),
            (subprocess.CalledProcessError(-2, ["tool"]), FailureKind.COMMAND_INTERRUPTION),
        )
        for error, expected in cases:
            with self.subTest(expected=expected), patch("doubt.system.run.subprocess.run", side_effect=error):
                with self.assertRaises(OperationalError) as raised:
                    runner.run(["tool"])
            self.assertEqual(raised.exception.kind, expected)

    def test_command_rendering_redacts_sensitive_arguments(self):
        rendered = command_text_for(
            [
                "tool",
                "--token",
                "do-not-render",
                "client_secret=hidden",
                "--client-secret=also-hidden",
                "ordinary",
            ]
        )
        self.assertEqual(
            rendered,
            "tool --token '[redacted]' 'client_secret=[redacted]' '--client-secret=[redacted]' ordinary",
        )
        self.assertNotIn("do-not-render", rendered)
        self.assertNotIn("hidden", rendered)

    def test_command_validation_rejects_empty_nonstring_and_nul(self):
        runner = CommandRunner()
        for command in ([], [""], ["tool", 1], ["tool", "bad\0value"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                runner.run(command)  # type: ignore[arg-type]

    def test_keyboard_interrupt_remains_visible(self):
        runner = CommandRunner()
        with patch("doubt.system.run.subprocess.run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.run(["tool"])

    def test_signalled_child_is_reaped_and_reported_as_interrupted(self):
        runner = CommandRunner()
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "pid"
            script = (
                "import os, signal, sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "os.kill(os.getpid(), signal.SIGTERM)\n"
            )
            with self.assertRaises(OperationalError) as raised:
                runner.run([sys.executable, "-c", script, str(pid_path)])
            pid = int(pid_path.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertEqual(raised.exception.kind, FailureKind.COMMAND_INTERRUPTION)

    def test_interruption_kills_and_reaps_provider_that_ignores_termination(self):
        process = Mock()
        process.pid = 1234
        process.poll.side_effect = [None, None]
        process.wait.side_effect = [subprocess.TimeoutExpired("tool", 2), 0]
        process.stdout = MagicMock()
        process.stdout.__enter__.return_value = process.stdout
        process.stdout.read.side_effect = KeyboardInterrupt
        runner = CommandRunner()

        with (
            patch("doubt.system.run.subprocess.Popen", return_value=process) as popen,
            patch("doubt.system.run.os.killpg") as kill_group,
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.run(["tool"], quiet=True)

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(
            kill_group.call_args_list,
            [call(1234, signal.SIGTERM), call(1234, signal.SIGKILL)],
        )
        self.assertEqual(process.wait.call_count, 2)

    def test_cleanup_handles_graceful_exit_prior_exit_and_repeated_call(self):
        graceful = Mock(pid=12)
        graceful.poll.side_effect = [None, 0]
        prior = Mock(pid=13)
        prior.poll.return_value = 0
        with patch("doubt.system.run.os.killpg") as kill_group:
            _terminate_and_reap(graceful)
            _terminate_and_reap(graceful)
            _terminate_and_reap(prior)
        kill_group.assert_called_once_with(12, signal.SIGTERM)
        graceful.wait.assert_called_once_with(timeout=TERMINATION_TIMEOUT_SECONDS)
        prior.wait.assert_not_called()

    def test_cleanup_preserves_original_interruption_if_post_kill_wait_times_out(self):
        process = Mock(pid=14)
        process.poll.side_effect = [None, None, None]
        process.wait.side_effect = subprocess.TimeoutExpired("tool", 2)
        with patch("doubt.system.run.os.killpg") as kill_group:
            _terminate_and_reap(process)
        self.assertEqual(
            kill_group.call_args_list,
            [call(14, signal.SIGTERM), call(14, signal.SIGKILL)],
        )
        self.assertEqual(process.wait.call_count, 2)

    def test_quiet_and_verbose_interruptions_share_bounded_cleanup_with_large_or_no_output(self):
        for details, output in ((False, b""), (True, b"x" * 100_000)):
            with self.subTest(details=details):
                process = Mock(pid=15)
                process.poll.side_effect = [None, None]
                process.wait.side_effect = [subprocess.TimeoutExpired("tool", 2), 0]
                process.stdout = MagicMock()
                process.stdout.__enter__.return_value = process.stdout
                process.stdout.read.side_effect = (
                    KeyboardInterrupt() if not output else [output, KeyboardInterrupt()]
                )
                with (
                    patch("doubt.system.run.subprocess.Popen", return_value=process),
                    patch("doubt.system.run.os.killpg") as kill_group,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        CommandRunner(details=details).run(["tool"], quiet=True)
                self.assertEqual(kill_group.call_count, 2)

    def test_cleanup_does_not_signal_unrelated_process_group(self):
        process = Mock(pid=21)
        process.poll.return_value = 0
        with patch("doubt.system.run.os.killpg") as kill_group:
            _terminate_and_reap(process)
        kill_group.assert_not_called()

    def test_stubborn_child_is_force_killed_reaped_and_cleanup_is_bounded(self):
        script = (
            "import signal\n"
            "signal.signal(signal.SIGTERM, lambda *_: None)\n"
            "print('ready', flush=True)\n"
            "while True: signal.pause()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self.assertIsNotNone(process.stdout)
            self.assertEqual(process.stdout.readline(), b"ready\n")
            started = time.monotonic()
            _terminate_and_reap(process)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, TERMINATION_TIMEOUT_SECONDS * 2 + 1)
            self.assertIsNotNone(process.returncode)
            self.assertLess(process.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(process.pid, 0)
            _terminate_and_reap(process)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)

    def test_details_and_notices_use_injected_writer(self):
        output = StringIO()
        runner = CommandRunner(details=True, writer=lambda line: print(line, file=output))
        with patch(
            "doubt.system.run.subprocess.run",
            return_value=subprocess.CompletedProcess(["tool"], 0),
        ):
            runner.succeeds(["tool", "argument"])
        runner.notice("first", "second")
        self.assertEqual(output.getvalue(), "inspect: tool argument\nfirst\nsecond\n")


if __name__ == "__main__":
    unittest.main()
