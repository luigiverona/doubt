from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import mock_open, patch

from doubt.core.failure import FailureKind, OperationalError
from doubt.ui.prompt import confirm, heading, read, startup


class ConfirmationTests(unittest.TestCase):
    def test_only_explicit_yes_confirms(self):
        for value in ("y", "Y", "yes", "YES"):
            with self.subTest(value=value):
                self.assertTrue(confirm(lambda _prompt, answer=value: answer, StringIO()))
        for value in ("", "n", "no", "anything"):
            with self.subTest(value=value):
                self.assertFalse(confirm(lambda _prompt, answer=value: answer, StringIO()))

    def test_confirmation_uses_dev_tty_once(self):
        terminal = mock_open(read_data="y\n")()
        with patch("builtins.open", return_value=terminal) as opened:
            self.assertTrue(confirm())
        opened.assert_called_once_with("/dev/tty", "r+", encoding="utf-8", buffering=1)
        terminal.write.assert_called_once_with("Continue? [y/N] ")
        terminal.readline.assert_called_once_with()

    def test_confirmation_failure_is_actionable_before_mutation(self):
        with patch("builtins.open", side_effect=OSError("no tty")):
            with self.assertRaises(OperationalError) as raised:
                confirm(stdout=StringIO())
        self.assertEqual(raised.exception.kind, FailureKind.BLOCKED_PRECONDITION)
        self.assertIn("controlling terminal", str(raised.exception))

    def test_read_also_uses_the_controlling_terminal(self):
        terminal = mock_open(read_data="value\n")()
        with patch("builtins.open", return_value=terminal):
            self.assertEqual(read("Value: "), "value")

    def test_confirmation_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(confirm(lambda _prompt: "n", StringIO()))
            self.assertEqual(list(root.iterdir()), [])

    def test_bootstrap_inherited_tty_reads_standard_input(self):
        output = StringIO()
        with (
            patch.dict("os.environ", {"DOUBT_CONFIRM_FD": "0"}),
            patch("sys.stdin", StringIO("yes\n")),
            redirect_stdout(output),
        ):
            self.assertTrue(confirm())
        self.assertEqual(output.getvalue(), "Continue? [y/N] ")

    def test_heading_and_startup_render_once_and_return_action(self):
        output = StringIO()
        heading(output=output, version="1.0.2")
        self.assertEqual(output.getvalue(), "Doubt 1.0.2\n\n")
        output = StringIO()
        self.assertEqual(startup(lambda: "loaded", ("deps", "apps"), output, "1.0.2"), "loaded")
        self.assertIn("run:\ndependencies, applications, setup, verification", output.getvalue())

    def test_keyboard_interrupt_prints_prompt_boundary(self):
        output = StringIO()
        with self.assertRaises(KeyboardInterrupt):
            confirm(lambda _prompt: (_ for _ in ()).throw(KeyboardInterrupt()), output)
        self.assertEqual(output.getvalue(), "\n")


if __name__ == "__main__":
    unittest.main()
