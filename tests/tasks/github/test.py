import unittest
from unittest.mock import patch

from doubt.core.failure import FailureKind, OperationalError
from doubt.system.run import CommandResult
from doubt.tasks.github import task as github


class FakeRunner:
    def __init__(self):
        self.dry_run = False
        self.commands = []
        self.available = {"gh"}
        self.auth_statuses = []
        self.protocol = "ssh"
        self.protocol_get_returncode = 0
        self.set_updates_protocol = True
        self.capture_commands = []

    def command_exists(self, command):
        return command in self.available

    def succeeds(self, command):
        if command == ["gh", "auth", "status"]:
            if self.auth_statuses:
                return self.auth_statuses.pop(0)
            return False
        return False

    def run(self, command, cwd=None, **_kwargs):
        self.commands.append((list(command), cwd))
        if command == github.PROTOCOL_SET_COMMAND and self.set_updates_protocol:
            self.protocol = "ssh"
            self.protocol_get_returncode = 0

    def capture(self, command, env=None):
        command = list(command)
        self.capture_commands.append(command)
        if command == github.PROTOCOL_GET_COMMAND:
            return CommandResult(
                self.protocol_get_returncode,
                f"{self.protocol}\n" if self.protocol else "",
            )
        return CommandResult(1, stderr="unexpected fake command")


class GitHubTaskTests(unittest.TestCase):
    def test_authenticated_status_returns_ok(self):
        runner = FakeRunner()
        runner.auth_statuses = [True]

        results = github.run(runner)

        self.assertEqual([result.status for result in results], ["ok", "ok"])
        self.assertEqual(runner.commands, [])

    def test_unauthenticated_dry_run_reports_pending_login_without_running_login(self):
        runner = FakeRunner()
        runner.dry_run = True
        runner.auth_statuses = [False]

        results = github.run(runner)

        self.assertEqual([result.status for result in results], ["add", "ok"])
        self.assertEqual(runner.commands, [])

    def test_unauthenticated_real_run_calls_login_and_returns_add_after_verification(self):
        runner = FakeRunner()
        runner.auth_statuses = [False, True]

        results = github.run(runner)

        self.assertEqual([result.status for result in results], ["add", "ok"])
        self.assertEqual(runner.commands, [(github.AUTH_LOGIN_COMMAND, None)])

    def test_failed_post_login_auth_check_returns_fail(self):
        runner = FakeRunner()
        runner.auth_statuses = [False, False]

        results = github.run(runner)

        self.assertEqual([result.status for result in results], ["fail"])
        self.assertEqual(runner.commands, [(github.AUTH_LOGIN_COMMAND, None)])

    def test_login_command_failure_is_operational_and_interruption_propagates(self):
        runner = FakeRunner()
        runner.auth_statuses = [False]
        with patch.object(
            runner,
            "run",
            side_effect=OperationalError(
                FailureKind.COMMAND_FAILURE,
                "github",
                "GitHub authentication failed",
            ),
        ):
            with self.assertRaises(OperationalError) as raised:
                github.run(runner)
        self.assertEqual(raised.exception.kind, FailureKind.COMMAND_FAILURE)

        runner.auth_statuses = [False]
        with patch.object(runner, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                github.run(runner)

    def test_missing_gh_fails_clearly(self):
        runner = FakeRunner()
        runner.available.remove("gh")

        with self.assertRaisesRegex(RuntimeError, "github-cli is required"):
            github.run(runner)

        self.assertEqual(runner.commands, [])

    def test_https_protocol_is_set_and_verified_in_real_mode(self):
        runner = FakeRunner()
        runner.auth_statuses = [True]
        runner.protocol = "https"

        results = github.run(runner)

        self.assertEqual([result.status for result in results], ["ok", "add"])
        self.assertEqual(runner.commands, [(github.PROTOCOL_SET_COMMAND, None)])
        self.assertEqual(
            runner.capture_commands,
            [github.PROTOCOL_GET_COMMAND, github.PROTOCOL_GET_COMMAND],
        )

    def test_https_protocol_dry_run_plans_change_without_set(self):
        runner = FakeRunner()
        runner.dry_run = True
        runner.auth_statuses = [True]
        runner.protocol = "https"

        results = github.run(runner)

        self.assertEqual([result.status for result in results], ["ok", "add"])
        self.assertEqual(results[-1].name, github.PROTOCOL_RESULT_NAME)
        self.assertEqual(runner.commands, [])
        self.assertEqual(runner.capture_commands, [github.PROTOCOL_GET_COMMAND])

    def test_missing_protocol_is_reconciled_to_ssh(self):
        runner = FakeRunner()
        runner.auth_statuses = [True]
        runner.protocol = ""
        runner.protocol_get_returncode = 1

        results = github.run(runner)

        self.assertEqual(results[-1].status, "add")
        self.assertEqual(runner.commands, [(github.PROTOCOL_SET_COMMAND, None)])

    def test_protocol_probe_failure_does_not_mutate(self):
        runner = FakeRunner()
        runner.auth_statuses = [True]
        with patch.object(
            runner,
            "capture",
            return_value=CommandResult(1, stderr="injected probe failure"),
        ):
            results = github.run(runner)
        self.assertEqual(results[-1].status, "fail")
        self.assertEqual(runner.commands, [])

    def test_unexpected_protocol_fails_without_mutation(self):
        runner = FakeRunner()
        runner.auth_statuses = [True]
        runner.protocol = "git"

        results = github.run(runner)

        self.assertEqual(results[-1].status, "fail")
        self.assertIn("unexpected", results[-1].name)
        self.assertEqual(runner.commands, [])

    def test_post_set_verification_failure_returns_fail(self):
        runner = FakeRunner()
        runner.auth_statuses = [True]
        runner.protocol = "https"
        runner.set_updates_protocol = False

        results = github.run(runner)

        self.assertEqual(results[-1].status, "fail")
        self.assertEqual(runner.commands, [(github.PROTOCOL_SET_COMMAND, None)])

    def test_second_real_run_is_idempotent(self):
        runner = FakeRunner()
        runner.auth_statuses = [True, True]
        runner.protocol = "https"

        first = github.run(runner)
        second = github.run(runner)

        self.assertEqual(first[-1].status, "add")
        self.assertEqual(second[-1].status, "ok")
        self.assertEqual(runner.commands, [(github.PROTOCOL_SET_COMMAND, None)])

    def test_protocol_reconciliation_never_uses_git_remotes_or_setup_git(self):
        runner = FakeRunner()
        runner.auth_statuses = [True]
        runner.protocol = "https"

        github.run(runner)

        commands = runner.capture_commands + [command for command, _ in runner.commands]
        self.assertFalse(any(command[:2] == ["git", "remote"] for command in commands))
        self.assertFalse(any(command == ["gh", "auth", "setup-git"] for command in commands))
        self.assertFalse(any(command[:3] == ["gh", "api", "user/keys"] for command in commands))
        self.assertFalse(any(command[0] == "ssh-keygen" for command in commands))
        self.assertTrue(all("github.com" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
