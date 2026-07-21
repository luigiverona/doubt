from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from doubt.cli import CliError, help_text, main, parse_command
from doubt.core.plan import Mode, parse, resolve
from doubt.packages.lists import DesiredState


class CommandInterfaceTests(unittest.TestCase):
    def state(self) -> DesiredState:
        return DesiredState(Path("root"), Path("apps"), Path("deps"), False)

    def parse(self, *values: str):
        with patch("doubt.cli.active_state", return_value=self.state()):
            return parse_command(values)

    def test_public_workstation_commands_are_small(self):
        self.assertEqual(self.parse().request.mode, Mode.MUTATE)
        self.assertEqual(self.parse("plan").request.mode, Mode.PLAN)
        self.assertEqual(self.parse("verify").request.mode, Mode.VERIFY)
        self.assertTrue(self.parse("--verbose").request.details)
        self.assertTrue(self.parse("plan", "--verbose").request.details)

    def test_package_commands_map_exact_operands(self):
        listing = self.parse("pkg", "list", "pacman", "browser").package
        addition = self.parse("pkg", "add", "pacman", "browser", "firefox").package
        removal = self.parse("pkg", "remove", "pacman", "firefox").package
        check = self.parse("pkg", "check").package
        self.assertEqual((listing.action, listing.source, listing.category), ("list", "pacman", "browser"))
        self.assertEqual(
            (addition.action, addition.source, addition.category, addition.package),
            ("add", "pacman", "browser", "firefox"),
        )
        self.assertEqual((removal.action, removal.source, removal.package), ("remove", "pacman", "firefox"))
        self.assertEqual(check.action, "check")

    def test_removed_commands_and_flags_are_rejected(self):
        for values in (
            ("tasks",),
            ("help",),
            ("-h",),
            ("--only", "apps"),
            ("--except", "apps"),
            ("--details",),
            ("--dry-run",),
            ("pkg", "add", "pacman", "browser", "firefox", "--dry-run"),
        ):
            with self.subTest(values=values), self.assertRaises(CliError):
                self.parse(*values)

    def test_unknown_and_malformed_commands_are_rejected(self):
        for values in (
            ("unknown",),
            ("plan", "extra"),
            ("pkg",),
            ("pkg", "unknown"),
            ("pkg", "add", "pacman"),
            ("pkg", "remove", "pacman"),
            ("pkg", "check", "extra"),
        ):
            with self.subTest(values=values), self.assertRaises(CliError):
                self.parse(*values)

    def test_help_is_canonical_concise_and_selector_free(self):
        help_output = help_text()
        self.assertLessEqual(len(help_output.splitlines()), 24)
        for value in ("./install plan", "./install verify", "pkg add SOURCE CATEGORY PACKAGE", "--verbose"):
            self.assertIn(value, help_output)
        for value in ("tasks", "doubt help", "--only", "--except", "--details", "--dry-run"):
            self.assertNotIn(value, help_output)
        self.assertNotIn("  -h", help_output)

    def test_installed_help_uses_doubt_name(self):
        with patch("doubt.cli.runtime_installed", return_value=True):
            parsed = parse_command(("--help",))
        self.assertTrue(parsed.installed)
        self.assertIn("  doubt\n", help_text(installed=True))
        self.assertNotIn("./install", help_text(installed=True))

    def test_help_does_not_inspect_workstation(self):
        with patch("doubt.cli.active_state") as state:
            parsed = parse_command(("--help",))
        state.assert_not_called()
        self.assertEqual(parsed.action, "help")

    def test_version_is_read_only_and_exact(self):
        stdout = StringIO()
        with patch("doubt.cli.active_state") as state, redirect_stdout(stdout):
            status = main(("--version",))
        state.assert_not_called()
        self.assertEqual((status, stdout.getvalue()), (0, "Doubt 1.0.5\n"))

    def test_main_maps_cli_error_to_status_two(self):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(("tasks",))
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "error: unknown command: tasks\n")

    def test_main_dispatches_help_without_application_execution(self):
        stdout = StringIO()
        with patch("doubt.cli.app.execute") as execute, redirect_stdout(stdout):
            status = main(("--help",))
        self.assertEqual(status, 0)
        execute.assert_not_called()
        self.assertEqual(stdout.getvalue().count("usage:"), 1)

    def test_internal_task_selection_api_remains_typed_and_validated(self):
        self.assertEqual(resolve(frozenset({"apps", "verify"})), ("apps", "verify"))
        self.assertNotIn("apps", resolve(excluded=frozenset({"apps"})))
        with self.assertRaisesRegex(ValueError, "cannot be used together"):
            resolve(frozenset({"apps"}), frozenset({"verify"}))
        self.assertEqual(parse("apps, verify"), frozenset({"apps", "verify"}))
        for value in ("", "apps,,verify"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "empty"):
                parse(value)
        with self.assertRaisesRegex(ValueError, "unknown task:"):
            parse("unknown")
        with self.assertRaisesRegex(ValueError, "unknown tasks:"):
            parse("unknown,other")


if __name__ == "__main__":
    unittest.main()
