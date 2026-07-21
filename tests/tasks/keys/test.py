import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from doubt.system.run import CommandResult
from doubt.tasks.github import keys as github_ssh
from doubt.tasks.github import task as github

CURRENT_KEY = "ssh-ed25519 AAAACURRENT"
OLD_KEY = "ssh-ed25519 AAAAOLD"
UNRELATED_KEY = "ssh-ed25519 AAAAUNRELATED"


class FakeRunner:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.authenticated = True
        self.login = "test-user"
        self.remote_keys = []
        self.calls = []
        self.next_id = 200
        self.api_scope = True
        self.upload_error = ""
        self.upload_errors = []
        self.delete_fails = False
        self.hide_uploaded_key = False
        self.change_old_after_upload = False
        self.uploaded_id = None
        self.ssh_result = CommandResult(
            1,
            stderr=("Hi test-user! You've successfully authenticated, but GitHub does not provide shell access."),
        )

    def command_exists(self, command):
        return command == "gh"

    def succeeds(self, command):
        return command == ["gh", "auth", "status"] and self.authenticated

    def run(self, command, cwd=None, **_kwargs):
        self.calls.append(("run", list(command)))
        if command == github.AUTH_LOGIN_COMMAND:
            self.authenticated = True
        if command == github_ssh.AUTH_REFRESH_COMMAND:
            self.api_scope = True

    def capture(self, command, env=None):
        command = list(command)
        self.calls.append(("capture", command, env))
        if command == ["gh", "api", "user"]:
            return CommandResult(0, json.dumps({"login": self.login}))
        if command == ["gh", "api", "--paginate", "--slurp", "user/keys"]:
            if not self.api_scope:
                return CommandResult(
                    1,
                    stderr=("This API operation needs the admin:public_key scope."),
                )
            keys = self.remote_keys
            if self.hide_uploaded_key and self.uploaded_id is not None:
                keys = [key for key in keys if key["id"] != self.uploaded_id]
            return CommandResult(0, json.dumps([keys]))
        if command[:5] == ["gh", "api", "--method", "POST", "user/keys"]:
            upload_error = self.upload_errors.pop(0) if self.upload_errors else self.upload_error
            if upload_error:
                return CommandResult(1, stderr=upload_error)
            title = command[command.index("-f") + 1].removeprefix("title=")
            key_field = command[command.index("-f", command.index("-f") + 1) + 1]
            key = key_field.removeprefix("key=")
            uploaded = {"id": self.next_id, "key": key, "title": title}
            self.next_id += 1
            self.uploaded_id = uploaded["id"]
            self.remote_keys.append(uploaded)
            if self.change_old_after_upload:
                for remote in self.remote_keys:
                    if remote["id"] == 10:
                        remote["key"] = UNRELATED_KEY
            return CommandResult(0, json.dumps(uploaded))
        if command[:4] == ["gh", "api", "--method", "DELETE"]:
            if self.delete_fails:
                return CommandResult(1, stderr="delete failed")
            key_id = int(command[-1].rsplit("/", 1)[-1])
            self.remote_keys = [key for key in self.remote_keys if key["id"] != key_id]
            return CommandResult(0)
        if command[:2] == ["ssh", "-T"]:
            return self.ssh_result
        return CommandResult(1, stderr="unexpected command")


class GitHubSshTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = Path(self.workspace.name)
        self.public_key = self.home / ".ssh/doubt.pub"
        self.public_key.parent.mkdir(mode=0o700)
        self.public_key.write_text(f"{CURRENT_KEY} local comment\n", encoding="utf-8")
        self.runner = FakeRunner()

    def tearDown(self):
        self.workspace.cleanup()

    def state_path(self):
        return self.home / github_ssh.STATE_RELATIVE_PATH

    def remote(self, key_id, key, title):
        return {"id": key_id, "key": key, "title": title}

    def state(self, key_id, key, title, previous=None):
        github_ssh.write_state(
            self.state_path(),
            github_ssh.OwnershipState(
                self.runner.login,
                github_ssh.RemoteKey(key_id, key, title),
                previous,
            ),
        )

    def mutation_commands(self):
        return [call[1] for call in self.runner.calls if call[0] == "capture" and "--method" in call[1]]

    def test_structured_listing_normalizes_comments(self):
        self.runner.remote_keys = [
            self.remote(1, f"{CURRENT_KEY} remote comment", "current"),
            self.remote(2, UNRELATED_KEY, "unrelated"),
        ]

        keys = github_ssh.list_remote_keys(self.runner)

        self.assertEqual(keys[0].public_key, CURRENT_KEY)
        self.assertEqual(keys[1].public_key, UNRELATED_KEY)

    def test_unowned_existing_key_preserves_user_title_without_adoption(self):
        self.runner.remote_keys = [
            self.remote(7, f"{CURRENT_KEY} existing comment", "user title"),
            self.remote(9, UNRELATED_KEY, "workstation"),
        ]

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "warn")
        self.assertIn("title preserved", results[-1].name)
        self.assertFalse(self.state_path().exists())
        self.assertEqual(self.mutation_commands(), [])
        self.assertEqual(len(self.runner.remote_keys), 2)

    def test_existing_canonical_key_is_adopted_without_remote_mutation(self):
        self.runner.remote_keys = [self.remote(7, CURRENT_KEY, "doubt")]

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        state = github_ssh.read_state(self.state_path())
        self.assertEqual(results[-1].status, "ok")
        self.assertEqual(state.remote_key, github_ssh.RemoteKey(7, CURRENT_KEY, "doubt"))
        self.assertEqual(self.mutation_commands(), [])

    def test_initial_upload_is_verified_and_persisted(self):
        self.runner.remote_keys = [self.remote(9, UNRELATED_KEY, "workstation")]

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        state = github_ssh.read_state(self.state_path())
        self.assertEqual(results[-1].status, "add")
        self.assertEqual(state.remote_key.key_id, 200)
        self.assertEqual(state.remote_key.public_key, CURRENT_KEY)
        self.assertEqual(state.remote_key.title, "doubt")
        self.assertEqual(self.runner.remote_keys[-1]["title"], "doubt")
        self.assertNotIn("doubt-managed", self.runner.remote_keys[-1]["title"])
        self.assertEqual(self.runner.remote_keys[0]["id"], 9)
        self.assertEqual(stat.S_IMODE(self.state_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_path().parent.stat().st_mode), 0o700)
        state_text = self.state_path().read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", state_text)
        self.assertNotIn("token", state_text.lower())

    def test_existing_noncanonical_title_is_preserved_without_compatibility_action(self):
        title = "personal workstation"
        self.runner.remote_keys = [self.remote(10, CURRENT_KEY, title)]
        self.state(10, CURRENT_KEY, title)
        state_before = self.state_path().read_bytes()

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "ok")
        self.assertEqual(self.mutation_commands(), [])
        self.assertEqual(self.runner.remote_keys, [self.remote(10, CURRENT_KEY, title)])
        self.assertEqual(self.state_path().read_bytes(), state_before)

    def test_noncanonical_title_without_state_never_authorizes_deletion(self):
        title = "unowned workstation"
        self.runner.remote_keys = [self.remote(10, CURRENT_KEY, title)]

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "warn")
        self.assertEqual(self.mutation_commands(), [])
        self.assertEqual(self.runner.remote_keys, [self.remote(10, CURRENT_KEY, title)])

    def test_already_canonical_key_is_idempotent_and_does_not_rewrite_state(self):
        self.runner.remote_keys = [self.remote(10, CURRENT_KEY, "doubt")]
        self.state(10, CURRENT_KEY, "doubt")

        with patch(
            "doubt.tasks.github.keys.write_state",
            wraps=github_ssh.write_state,
        ) as state_writer:
            first = github_ssh.synchronize(self.public_key, self.runner, self.home)
            second = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(first[-1].status, "ok")
        self.assertEqual(second[-1].status, "ok")
        self.assertEqual(self.mutation_commands(), [])
        state_writer.assert_not_called()

    def test_only_authentication_key_scope_and_endpoints_are_used(self):
        self.assertIn("admin:public_key", github_ssh.AUTH_REFRESH_COMMAND)
        self.assertNotIn("admin:ssh_signing_key", github_ssh.AUTH_REFRESH_COMMAND)
        self.runner.remote_keys = [self.remote(10, CURRENT_KEY, "doubt")]
        self.state(10, CURRENT_KEY, "doubt")

        github_ssh.synchronize(self.public_key, self.runner, self.home)

        commands = " ".join(" ".join(call[1]) for call in self.runner.calls)
        self.assertNotIn("ssh_signing", commands)

    def test_rotation_adds_and_verifies_before_deleting_exact_owned_key(self):
        old_title = "doubt-managed old"
        self.runner.remote_keys = [
            self.remote(10, OLD_KEY, old_title),
            self.remote(11, UNRELATED_KEY, "doubt-managed unrelated"),
        ]
        self.state(10, OLD_KEY, old_title)

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        mutations = self.mutation_commands()
        self.assertEqual(results[-1].status, "add")
        self.assertEqual(mutations[0][3:5], ["POST", "user/keys"])
        self.assertEqual(mutations[1][3:5], ["DELETE", "user/keys/10"])
        post_index = next(
            index for index, call in enumerate(self.runner.calls) if call[0] == "capture" and "POST" in call[1]
        )
        delete_index = next(
            index for index, call in enumerate(self.runner.calls) if call[0] == "capture" and "DELETE" in call[1]
        )
        self.assertTrue(
            any(
                call[0] == "capture" and call[1] == ["gh", "api", "--paginate", "--slurp", "user/keys"]
                for call in self.runner.calls[post_index + 1 : delete_index]
            )
        )
        self.assertEqual({key["id"] for key in self.runner.remote_keys}, {11, 200})
        self.assertIsNone(github_ssh.read_state(self.state_path()).previous_remote_key)

    def test_missing_state_never_deletes_title_matched_key(self):
        self.runner.remote_keys = [
            self.remote(10, OLD_KEY, "doubt-managed old-machine"),
        ]

        github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual({key["id"] for key in self.runner.remote_keys}, {10, 200})
        self.assertFalse(any(command[3] == "DELETE" for command in self.mutation_commands()))

    def test_missing_recorded_remote_key_refreshes_state_without_delete(self):
        self.state(10, OLD_KEY, "owned old")

        github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual({key["id"] for key in self.runner.remote_keys}, {200})
        self.assertFalse(any(command[3] == "DELETE" for command in self.mutation_commands()))

    def test_mismatched_recorded_remote_key_blocks_all_mutation(self):
        self.runner.remote_keys = [self.remote(10, UNRELATED_KEY, "owned old")]
        self.state(10, OLD_KEY, "owned old")
        self.state_path().chmod(0o644)
        self.state_path().parent.chmod(0o755)

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "fail")
        self.assertEqual(self.mutation_commands(), [])
        self.assertEqual(self.runner.remote_keys, [self.remote(10, UNRELATED_KEY, "owned old")])
        self.assertEqual(stat.S_IMODE(self.state_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_path().parent.stat().st_mode), 0o700)

    def test_mismatched_recorded_title_blocks_all_mutation(self):
        self.runner.remote_keys = [self.remote(10, OLD_KEY, "renamed remotely")]
        self.state(10, OLD_KEY, "owned old")

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "fail")
        self.assertEqual(self.mutation_commands(), [])

    def test_upload_failure_preserves_old_owned_key_and_state(self):
        self.runner.remote_keys = [self.remote(10, OLD_KEY, "owned old")]
        self.state(10, OLD_KEY, "owned old")
        self.runner.upload_error = "key is already in use"

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "fail")
        self.assertEqual(self.runner.remote_keys, [self.remote(10, OLD_KEY, "owned old")])
        self.assertEqual(github_ssh.read_state(self.state_path()).remote_key.key_id, 10)
        self.assertIn("another account", results[-1].name)

    def test_last_moment_ownership_mismatch_prevents_deletion(self):
        self.runner.remote_keys = [self.remote(10, OLD_KEY, "owned old")]
        self.state(10, OLD_KEY, "owned old")
        self.runner.change_old_after_upload = True

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "fail")
        self.assertEqual({key["id"] for key in self.runner.remote_keys}, {10, 200})
        self.assertFalse(any(command[3] == "DELETE" for command in self.mutation_commands()))
        state = github_ssh.read_state(self.state_path())
        self.assertEqual(state.remote_key.key_id, 200)
        self.assertEqual(state.previous_remote_key.key_id, 10)

    def test_upload_verification_failure_preserves_old_owned_key(self):
        self.runner.remote_keys = [self.remote(10, OLD_KEY, "owned old")]
        self.state(10, OLD_KEY, "owned old")
        self.runner.hide_uploaded_key = True

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "fail")
        self.assertIn(10, {key["id"] for key in self.runner.remote_keys})
        self.assertFalse(any(command[3] == "DELETE" for command in self.mutation_commands()))

    def test_delete_failure_preserves_new_key_and_records_stale_key(self):
        self.runner.remote_keys = [self.remote(10, OLD_KEY, "owned old")]
        self.state(10, OLD_KEY, "owned old")
        self.runner.delete_fails = True

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        state = github_ssh.read_state(self.state_path())
        self.assertEqual(results[-1].status, "warn")
        self.assertEqual({key["id"] for key in self.runner.remote_keys}, {10, 200})
        self.assertEqual(state.remote_key.key_id, 200)
        self.assertEqual(state.previous_remote_key.key_id, 10)

    def test_missing_current_remote_retains_proof_and_cleans_recorded_stale_key(self):
        previous = github_ssh.RemoteKey(10, OLD_KEY, "owned old")
        self.state(99, CURRENT_KEY, "missing current", previous)
        self.runner.remote_keys = [self.remote(10, OLD_KEY, "owned old")]

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "add")
        self.assertEqual({key["id"] for key in self.runner.remote_keys}, {200})
        state = github_ssh.read_state(self.state_path())
        self.assertEqual(state.remote_key.key_id, 200)
        self.assertIsNone(state.previous_remote_key)

    def test_dry_run_reports_rotation_without_remote_or_state_mutation(self):
        self.runner.dry_run = True
        self.runner.remote_keys = [self.remote(10, OLD_KEY, "owned old")]
        self.state(10, OLD_KEY, "owned old")
        before = self.state_path().read_bytes()

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].name, "replace previously owned GitHub SSH key")
        self.assertEqual(self.mutation_commands(), [])
        self.assertEqual(self.state_path().read_bytes(), before)
        self.assertEqual(self.runner.remote_keys, [self.remote(10, OLD_KEY, "owned old")])

    def test_dry_run_unauthenticated_does_not_login_or_call_api(self):
        self.runner.dry_run = True
        self.runner.authenticated = False

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].name, "GitHub authentication required for SSH key sync")
        self.assertEqual(self.runner.calls, [])

    def test_real_unauthenticated_run_reuses_github_login(self):
        self.runner.authenticated = False

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "add")
        self.assertIn(("run", github.AUTH_LOGIN_COMMAND), self.runner.calls)

    def test_real_run_requests_missing_github_key_scope_and_retries(self):
        self.runner.api_scope = False

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].status, "add")
        self.assertIn(("run", github_ssh.AUTH_REFRESH_COMMAND), self.runner.calls)

    def test_dry_run_reports_missing_github_key_scope_without_refresh(self):
        self.runner.dry_run = True
        self.runner.api_scope = False

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].name, "GitHub SSH key API permission required")
        self.assertFalse(any(call[0] == "run" for call in self.runner.calls))

    def test_dry_run_existing_remote_key_plans_state_adoption(self):
        self.runner.dry_run = True
        self.runner.remote_keys = [self.remote(7, CURRENT_KEY, "doubt")]

        results = github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(results[-1].name, "adopt existing GitHub SSH key ownership state")
        self.assertFalse(self.state_path().exists())
        self.assertEqual(self.mutation_commands(), [])

    def test_state_symlink_is_rejected_without_remote_calls(self):
        self.state_path().parent.mkdir(parents=True)
        target = self.home / "target"
        target.write_text("{}", encoding="utf-8")
        self.state_path().symlink_to(target)

        with self.assertRaisesRegex(RuntimeError, "unsafe GitHub SSH ownership state path"):
            github_ssh.synchronize(self.public_key, self.runner, self.home)

        self.assertEqual(self.runner.calls, [])

    def test_documented_github_ssh_success_with_exit_one_is_accepted(self):
        results = github_ssh.verify_github_ssh(self.runner, "verified", "ok")

        self.assertEqual(results[-1].status, "ok")
        ssh_call = self.runner.calls[-1]
        self.assertEqual(ssh_call[2], {"LC_ALL": "C"})
        self.assertIn("BatchMode=yes", ssh_call[1])
        self.assertIn("StrictHostKeyChecking=yes", ssh_call[1])
        self.assertEqual(ssh_call[1][-1], "git@github.com")
        self.assertIn("github.com", github_ssh.AUTH_REFRESH_COMMAND)

    def test_permission_denied_and_unexpected_output_are_rejected(self):
        for response in (
            CommandResult(255, stderr="Permission denied (publickey)."),
            CommandResult(1, stderr="unexpected response"),
        ):
            with self.subTest(response=response):
                self.runner.ssh_result = response
                results = github_ssh.verify_github_ssh(self.runner, "verified", "ok")
                self.assertEqual(results[-1].status, "fail")

    def test_planning_classifies_authentication_scope_and_api_failures(self):
        with patch("doubt.tasks.github.keys.task.is_authenticated", return_value=False):
            results = github_ssh.plan_after_local_reconciliation(self.runner)
        self.assertEqual(results[0].status, "add")

        for error, expected in (
            ("admin:public_key is required", "GitHub SSH key API permission required"),
            ("service unavailable", "inspect GitHub SSH keys"),
        ):
            with (
                self.subTest(error=error),
                patch("doubt.tasks.github.keys.task.is_authenticated", return_value=True),
                patch("doubt.tasks.github.keys.fetch_remote_keys", return_value=(None, error)),
            ):
                results = github_ssh.plan_after_local_reconciliation(self.runner)
            self.assertEqual(results[0].name, expected)

    def test_remote_listing_rejects_each_malformed_response_shape(self):
        cases = (
            CommandResult(1, stderr="failed"),
            CommandResult(0, "{}"),
            CommandResult(0, "[[1]]"),
            CommandResult(0, '[[{"id":"bad","key":"ssh-ed25519 AAAA","title":"title"}]]'),
            CommandResult(0, '[[{"id":1,"key":"invalid","title":"title"}]]'),
        )
        for response in cases:
            runner = Mock()
            runner.capture.return_value = response
            with self.subTest(response=response):
                keys, error = github_ssh.fetch_remote_keys(runner)
            self.assertIsNone(keys)
            self.assertTrue(error)

    def test_public_key_and_recorded_key_defensive_reads(self):
        self.assertEqual(github_ssh.read_public_identity(self.home / "missing"), "")
        invalid = self.home / "invalid.pub"
        invalid.write_text("invalid\n", encoding="utf-8")
        self.assertEqual(github_ssh.read_public_identity(invalid), "")
        decode_error = UnicodeDecodeError("utf-8", b"x", 0, 1, "bad")
        with patch("pathlib.Path.read_text", side_effect=decode_error):
            self.assertEqual(github_ssh.read_public_identity(invalid), "")
        recorded = github_ssh.RemoteKey(1, CURRENT_KEY, "title")
        remote, error = github_ssh.verify_recorded_key(recorded, None)
        self.assertIsNone(remote)
        self.assertIn("ownership verification", error)
        self.assertIsNone(github_ssh.parse_json_response(CommandResult(1, "{}")))
        self.assertIsNone(github_ssh.parse_json_response(CommandResult(0, "not json")))


if __name__ == "__main__":
    unittest.main()
