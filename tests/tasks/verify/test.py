import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubt.packages.lists import PackageList
from doubt.system.run import CommandResult
from doubt.tasks import git as git_task
from doubt.tasks import path as path_task
from doubt.tasks import ssh, verify
from doubt.tasks.github import keys as github_ssh
from doubt.tasks.github import task as github

PUBLIC_IDENTITY = "ssh-ed25519 AAAATESTDOUBTKEY"


class FakeRunner:
    def __init__(self, home):
        self.home = Path(home)
        self.dry_run = False
        self.available = {"gh", "git", "pacman", "yay", "flatpak", "ssh", "ssh-keygen"}
        self.installed = {"git", "github-cli", "firefox", "code-bin", "example.App"}
        self.authenticated = True
        self.account = "test-user"
        self.protocol = "ssh"
        self.git_values = {
            "user.name": "Test User",
            "user.email": "test@example.com",
            "init.defaultBranch": "main",
        }
        self.remote_keys = [github_ssh.RemoteKey(100, PUBLIC_IDENTITY, "doubt")]
        self.package_check_errors = {}
        self.derived_identity = PUBLIC_IDENTITY
        self.effective_config = "user git\nhostname github.com\nidentityfile ~/.ssh/doubt\nidentitiesonly yes\n"
        self.ssh_auth = CommandResult(
            1,
            stderr=("Hi test-user! You've successfully authenticated, but GitHub does not provide shell access."),
        )
        self.calls = []

    def home_directory(self):
        return self.home

    def command_exists(self, command):
        return command in self.available

    def succeeds(self, command):
        self.calls.append(("succeeds", list(command)))
        if command == verify.VERIFY_AUTH_COMMAND:
            return self.authenticated
        return False

    def output(self, command):
        self.calls.append(("output", list(command)))
        if command[:2] == ["ssh-keygen", "-y"]:
            return self.derived_identity
        if command[:2] == ["ssh", "-G"]:
            return self.effective_config
        return ""

    def capture(self, command, env=None):
        self.calls.append(("capture", list(command)))
        if command[:2] in (["pacman", "-Qi"], ["yay", "-Q"], ["flatpak", "info"]):
            package = command[2]
            return CommandResult(self.package_check_errors.get(package, 0 if package in self.installed else 1))
        if command == ["gh", "api", "user"]:
            return CommandResult(0, json.dumps({"login": self.account}))
        if command == github.PROTOCOL_GET_COMMAND:
            return CommandResult(0, self.protocol + "\n")
        if command == ["gh", "api", "--paginate", "--slurp", "user/keys"]:
            data = [[{"id": key.key_id, "key": key.public_key, "title": key.title} for key in self.remote_keys]]
            return CommandResult(0, json.dumps(data))
        if command[:4] == ["git", "config", "--global", "--get"]:
            value = self.git_values.get(command[4])
            return CommandResult(0, value + "\n") if value is not None else CommandResult(1)
        if command[:2] == ["ssh", "-T"]:
            return self.ssh_auth
        return CommandResult(1, stderr="unexpected command")

    def run(self, command, cwd=None):
        raise AssertionError(f"verification attempted mutation: {command}")


class FinalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = Path(self.workspace.name)
        self.runner = FakeRunner(self.home)
        self.conflict_verification = patch(
            "doubt.tasks.verify.conflicts.verify_conflicts",
            return_value=verify.result("package conflicts", "ok"),
        )
        self.conflict_verification.start()
        self.codex_verification = patch(
            "doubt.tasks.verify.codex.verify_state",
            return_value=[verify.result("Codex dual-account setup", "ok")],
        )
        self.codex_verification.start()
        self.dependencies = [
            PackageList("pacman", "bootstrap", ("git",), Path("deps/pacman/bootstrap")),
            PackageList("pacman", "github", ("github-cli",), Path("deps/pacman/github")),
        ]
        self.applications = [
            PackageList("pacman", "browser", ("firefox",), Path("apps/pacman/browser")),
            PackageList("aur", "dev", ("code-bin",), Path("apps/aur/dev")),
            PackageList("flatpak", "chat", ("example.App",), Path("apps/flatpak/chat")),
        ]
        self.create_valid_state()

    def tearDown(self):
        self.conflict_verification.stop()
        self.codex_verification.stop()
        self.workspace.cleanup()

    def create_valid_state(self):
        ssh_directory = self.home / ".ssh"
        ssh_directory.mkdir(mode=0o700)
        private = ssh_directory / ssh.PRIVATE_KEY_NAME
        public = ssh_directory / ssh.PUBLIC_KEY_NAME
        private.write_text("private test marker", encoding="utf-8")
        private.chmod(0o600)
        public.write_text(PUBLIC_IDENTITY + " doubt-managed\n", encoding="utf-8")
        public.chmod(0o644)
        managed = ssh_directory / ssh.MANAGED_CONFIG_NAME
        managed.write_text(ssh.MANAGED_CONFIG, encoding="utf-8")
        managed.chmod(0o600)
        known_hosts = ssh_directory / ssh.KNOWN_HOSTS_NAME
        known_hosts.write_text(ssh.GITHUB_KNOWN_HOSTS, encoding="ascii")
        known_hosts.chmod(0o600)
        user_config = ssh_directory / ssh.SSH_CONFIG_NAME
        user_config.write_text(ssh.INCLUDE_DIRECTIVE + "\n", encoding="utf-8")
        user_config.chmod(0o600)

        config = self.home / git_task.CONFIG_RELATIVE_PATH
        config.parent.mkdir(parents=True, mode=0o700)
        config.parent.chmod(0o700)
        config.write_text(
            '[git]\nname = "Test User"\nemail = "test@example.com"\ndefault_branch = "main"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)

        state = github_ssh.OwnershipState(
            "test-user",
            github_ssh.RemoteKey(100, PUBLIC_IDENTITY, "doubt"),
        )
        github_ssh.write_state(self.home / github_ssh.STATE_RELATIVE_PATH, state)
        fragment = path_task.target(self.home, {})
        fragment.parent.mkdir(parents=True)
        fragment.write_text(path_task.CONTENT, encoding="utf-8")
        fragment.chmod(path_task.MODE)

    def status_for(self, results, text):
        return next(result.status for result in results if text in result.name)

    def test_complete_desired_state_succeeds_without_mutation(self):
        results = verify.run(
            self.applications,
            self.dependencies,
            self.runner,
            home=self.home,
        )

        self.assertTrue(results)
        self.assertTrue(all(result.status == "ok" for result in results))
        self.assertFalse(any(call[0] == "run" for call in self.runner.calls))
        self.assertIn("dependency github-cli", [result.name for result in results])

    def test_missing_dependency_and_application_fail(self):
        self.runner.installed.remove("github-cli")
        self.runner.installed.remove("code-bin")

        results = verify.run(self.applications, self.dependencies, self.runner, home=self.home)

        self.assertEqual(self.status_for(results, "dependency github-cli"), "fail")
        self.assertEqual(self.status_for(results, "application code-bin"), "fail")

    def test_each_application_source_uses_its_specific_check(self):
        verify.verify_packages(self.applications, self.runner, "application")

        checks = [
            command
            for kind, command in self.runner.calls
            if kind == "capture" and command[0] in ("pacman", "yay", "flatpak")
        ]
        self.assertEqual(
            checks,
            [
                ["pacman", "-Qi", "firefox"],
                ["yay", "-Q", "code-bin"],
                ["flatpak", "info", "example.App"],
            ],
        )

    def test_unavailable_source_check_is_distinct_from_missing_package(self):
        self.runner.available.remove("yay")

        results = verify.verify_packages(self.applications, self.runner, "application")

        self.assertEqual(self.status_for(results, "requires yay"), "fail")
        self.assertFalse(any(result.name == "application code-bin" for result in results))

    def test_package_check_error_is_distinct_from_missing_package(self):
        self.runner.package_check_errors["code-bin"] = 2

        results = verify.verify_packages(self.applications, self.runner, "application")

        self.assertEqual(self.status_for(results, "check failed for code-bin"), "fail")
        self.assertFalse(any(result.name == "missing application code-bin" for result in results))

    def test_package_verification_order_is_deterministic(self):
        results = verify.verify_packages(self.applications, self.runner, "application")

        self.assertEqual(
            [result.name for result in results],
            ["application firefox", "application code-bin", "application example.App"],
        )

    def test_unauthenticated_and_https_github_state_fail_without_repair(self):
        self.runner.authenticated = False
        self.runner.protocol = "https"

        results, account = verify.verify_github(self.runner)

        self.assertEqual(account, "")
        self.assertTrue(all(result.status == "fail" for result in results))
        forbidden = (github.AUTH_LOGIN_COMMAND, github.PROTOCOL_SET_COMMAND, github_ssh.AUTH_REFRESH_COMMAND)
        commands = [command for _kind, command in self.runner.calls]
        self.assertTrue(all(command not in commands for command in forbidden))

    def test_github_requires_active_github_dot_com_account(self):
        self.runner.account = ""

        results, account = verify.verify_github(self.runner)

        self.assertEqual(account, "")
        self.assertEqual(results[0].status, "fail")

    def test_local_ssh_missing_and_wrong_permissions_fail(self):
        (self.home / ".ssh" / ssh.PRIVATE_KEY_NAME).unlink()
        results = verify.verify_local_ssh(self.home, self.runner)
        self.assertEqual(results[-1].status, "fail")

        private = self.home / ".ssh" / ssh.PRIVATE_KEY_NAME
        private.write_text("private test marker", encoding="utf-8")
        private.chmod(0o644)
        results = verify.verify_local_ssh(self.home, self.runner)
        self.assertEqual(self.status_for(results, "private key permissions"), "fail")

    def test_mismatched_keypair_fails(self):
        self.runner.derived_identity = "ssh-ed25519 AAAADIFFERENT"

        results = verify.verify_local_ssh(self.home, self.runner)

        self.assertEqual(self.status_for(results, "keypair"), "fail")

    def test_invalid_managed_config_and_include_fail(self):
        managed = self.home / ".ssh" / ssh.MANAGED_CONFIG_NAME
        managed.write_text("Host example.com\n", encoding="utf-8")
        results = verify.verify_local_ssh(self.home, self.runner)
        self.assertEqual(self.status_for(results, "client configuration differs"), "fail")

        managed.write_text(ssh.MANAGED_CONFIG, encoding="utf-8")
        config = self.home / ".ssh" / ssh.SSH_CONFIG_NAME
        config.write_text("Host example.com\n", encoding="utf-8")
        results = verify.verify_local_ssh(self.home, self.runner)
        self.assertEqual(self.status_for(results, "Include"), "fail")

        config.write_text(ssh.INCLUDE_DIRECTIVE + "\n" + ssh.INCLUDE_DIRECTIVE + "\n", encoding="utf-8")
        results = verify.verify_local_ssh(self.home, self.runner)
        self.assertEqual(self.status_for(results, "Include"), "fail")

    def test_incorrect_effective_ssh_configuration_fields_fail(self):
        replacements = (
            ("user git", "user wrong"),
            ("hostname github.com", "hostname example.com"),
            ("identityfile ~/.ssh/doubt", "identityfile ~/.ssh/other"),
            ("identitiesonly yes", "identitiesonly no"),
        )
        original = self.runner.effective_config
        for old, new in replacements:
            with self.subTest(field=old):
                self.runner.effective_config = original.replace(old, new)
                results = verify.verify_local_ssh(self.home, self.runner)
                self.assertEqual(self.status_for(results, "effective github.com"), "fail")

    def test_unsafe_ssh_symlink_fails_without_following_it(self):
        config = self.home / ".ssh" / ssh.MANAGED_CONFIG_NAME
        target = self.home / "outside"
        target.write_text(ssh.MANAGED_CONFIG, encoding="utf-8")
        config.unlink()
        config.symlink_to(target)

        results = verify.verify_local_ssh(self.home, self.runner)

        self.assertEqual(results[-1].status, "fail")
        self.assertEqual(target.read_text(encoding="utf-8"), ssh.MANAGED_CONFIG)

    def test_valid_github_ssh_state_ignores_unrelated_remote_keys(self):
        self.runner.remote_keys.append(github_ssh.RemoteKey(99, "ssh-ed25519 AAAAUNRELATED", "workstation"))

        result = verify.verify_github_ssh_state(self.home, "test-user", self.runner)

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(self.runner.remote_keys), 2)

    def test_missing_or_mismatched_github_ssh_state_fails(self):
        state_path = self.home / github_ssh.STATE_RELATIVE_PATH
        state_path.unlink()
        self.assertEqual(
            verify.verify_github_ssh_state(self.home, "test-user", self.runner).status,
            "fail",
        )
        github_ssh.write_state(
            state_path,
            github_ssh.OwnershipState(
                "other-user",
                github_ssh.RemoteKey(100, PUBLIC_IDENTITY, "doubt"),
            ),
        )
        result = verify.verify_github_ssh_state(self.home, "test-user", self.runner)
        self.assertIn("account", result.name)
        self.assertEqual(result.status, "fail")

    def test_remote_id_public_key_and_title_drift_fail(self):
        cases = (
            ([github_ssh.RemoteKey(200, PUBLIC_IDENTITY, "doubt")], "missing remotely"),
            ([github_ssh.RemoteKey(100, "ssh-ed25519 AAAADIFFERENT", "doubt")], "public key"),
            ([github_ssh.RemoteKey(100, PUBLIC_IDENTITY, "doubt-managed legacy")], "title differs"),
        )
        for remote_keys, message in cases:
            with self.subTest(message=message):
                self.runner.remote_keys = remote_keys
                result = verify.verify_github_ssh_state(self.home, "test-user", self.runner)
                self.assertEqual(result.status, "fail")
                self.assertIn(message, result.name)

    def test_state_public_key_mismatch_and_pending_cleanup_fail(self):
        state_path = self.home / github_ssh.STATE_RELATIVE_PATH
        for state, message in (
            (
                github_ssh.OwnershipState(
                    "test-user",
                    github_ssh.RemoteKey(100, "ssh-ed25519 AAAADIFFERENT", "doubt"),
                ),
                "public key",
            ),
            (
                github_ssh.OwnershipState(
                    "test-user",
                    github_ssh.RemoteKey(100, PUBLIC_IDENTITY, "doubt"),
                    github_ssh.RemoteKey(99, "ssh-ed25519 AAAAOLD", "old"),
                ),
                "pending",
            ),
        ):
            with self.subTest(message=message):
                github_ssh.write_state(state_path, state)
                result = verify.verify_github_ssh_state(self.home, "test-user", self.runner)
                self.assertEqual(result.status, "fail")
                self.assertIn(message, result.name)

    def test_github_ssh_state_permissions_and_symlink_fail(self):
        state_path = self.home / github_ssh.STATE_RELATIVE_PATH
        state_path.parent.chmod(0o755)
        self.assertIn(
            "directory permissions",
            verify.verify_github_ssh_state(self.home, "test-user", self.runner).name,
        )
        state_path.parent.chmod(0o700)
        state_path.chmod(0o644)
        self.assertIn(
            "permissions",
            verify.verify_github_ssh_state(self.home, "test-user", self.runner).name,
        )
        target = self.home / "outside-state"
        target.write_text(state_path.read_text(encoding="utf-8"), encoding="utf-8")
        state_path.unlink()
        state_path.symlink_to(target)
        result = verify.verify_github_ssh_state(self.home, "test-user", self.runner)
        self.assertEqual(result.status, "fail")
        self.assertIn("unsafe", result.name)

    def test_github_ssh_authentication_accepts_documented_exit_one_only(self):
        self.assertEqual(
            verify.verify_github_ssh_authentication(self.runner).status,
            "ok",
        )
        failures = (
            CommandResult(255, stderr="Permission denied (publickey)."),
            CommandResult(1, stderr="unexpected output"),
            CommandResult(255, stderr="Connection timed out"),
        )
        for response in failures:
            with self.subTest(response=response):
                self.runner.ssh_auth = response
                self.assertEqual(
                    verify.verify_github_ssh_authentication(self.runner).status,
                    "fail",
                )

    def test_managed_git_config_missing_invalid_modes_and_values_fail(self):
        config = self.home / git_task.CONFIG_RELATIVE_PATH
        config.unlink()
        self.assertEqual(verify.verify_git(self.home, self.runner)[0].status, "fail")

        config.write_text("not toml [", encoding="utf-8")
        config.chmod(0o600)
        self.assertEqual(verify.verify_git(self.home, self.runner)[0].status, "fail")

        config.write_text(
            '[git]\nname = "Test User"\nemail = "test@example.com"\ndefault_branch = "main"\n',
            encoding="utf-8",
        )
        config.chmod(0o644)
        self.assertIn("permissions", verify.verify_git(self.home, self.runner)[0].name)

    def test_each_managed_git_value_drift_fails_without_mutation(self):
        for key in self.runner.git_values:
            with self.subTest(key=key):
                original = self.runner.git_values[key]
                self.runner.git_values[key] = "different"
                result = verify.verify_git(self.home, self.runner)[0]
                self.assertEqual(result.status, "fail")
                self.assertIn(key, result.name)
                self.runner.git_values[key] = original

    def test_explicit_dry_run_converts_drift_to_warnings(self):
        self.runner.installed.remove("github-cli")
        self.runner.protocol = "https"

        results = verify.run(
            self.applications,
            self.dependencies,
            self.runner,
            home=self.home,
            warn_only=True,
        )

        self.assertFalse(any(result.status == "fail" for result in results))
        self.assertTrue(any(result.status == "warn" for result in results))

    def test_verification_never_calls_mutating_runner_method(self):
        with (
            patch("doubt.tasks.github.keys.write_state") as write_state,
            patch("doubt.tasks.git.write_config") as write_config,
            patch("doubt.tasks.ssh.atomic_write") as write_ssh_config,
        ):
            verify.run(self.applications, self.dependencies, self.runner, home=self.home)

        mutation_commands = [
            command
            for _kind, command in self.runner.calls
            if any(token in command for token in ("POST", "DELETE", "set", "login", "refresh"))
        ]
        self.assertEqual(mutation_commands, [])
        write_state.assert_not_called()
        write_config.assert_not_called()
        write_ssh_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
