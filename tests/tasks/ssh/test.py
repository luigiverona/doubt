import json
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from doubt.system.run import CommandResult, CommandRunner
from doubt.tasks import ssh
from doubt.ui.render import PresentationContext, render_report

PUBLIC_IDENTITY = "ssh-ed25519 AAAATESTDOUBTKEY"
OTHER_PUBLIC_IDENTITY = "ssh-ed25519 AAAAOTHERKEY"
PRIVATE_MARKER = "private-test-marker-never-rendered"


class FakeRunner:
    def __init__(self, home: Path, dry_run: bool = False):
        self.dry_run = dry_run
        self.home = home
        self.available = {"gh", "ssh", "ssh-keygen"}
        self.commands = []
        self.capture_commands = []
        self.output_commands = []
        self.public_keys = {}
        self.generated_public = PUBLIC_IDENTITY
        self.generate_files = True
        self.fail_derivation = False
        self.client_config_output = "user git\nhostname github.com\nidentityfile ~/.ssh/doubt\nidentitiesonly yes\n"
        self.remote_keys = [{"id": 100, "key": PUBLIC_IDENTITY, "title": "doubt"}]
        self.next_remote_id = 200
        self.delete_fails = False

    def home_directory(self):
        return self.home

    def command_exists(self, command):
        return command in self.available

    def succeeds(self, command):
        return command == ["gh", "auth", "status"]

    def capture(self, command, env=None):
        self.capture_commands.append(list(command))
        if command == ["gh", "api", "user"]:
            return CommandResult(0, json.dumps({"login": "test-user"}))
        if command == ["gh", "api", "--paginate", "--slurp", "user/keys"]:
            return CommandResult(0, json.dumps([self.remote_keys]))
        if command[:5] == ["gh", "api", "--method", "POST", "user/keys"]:
            title = command[command.index("-f") + 1].removeprefix("title=")
            key_field = command[command.index("-f", command.index("-f") + 1) + 1]
            key = key_field.removeprefix("key=")
            uploaded = {"id": self.next_remote_id, "key": key, "title": title}
            self.next_remote_id += 1
            self.remote_keys.append(uploaded)
            return CommandResult(0, json.dumps(uploaded))
        if command[:4] == ["gh", "api", "--method", "DELETE"]:
            if self.delete_fails:
                return CommandResult(1, stderr="delete failed")
            key_id = int(command[-1].rsplit("/", 1)[-1])
            self.remote_keys = [key for key in self.remote_keys if key["id"] != key_id]
            return CommandResult(0)
        if command[0:2] == ["ssh", "-T"]:
            return CommandResult(
                1,
                stderr=("Hi test-user! You've successfully authenticated, but GitHub does not provide shell access."),
            )
        return CommandResult(1, stderr="unexpected fake command")

    def output(self, command):
        self.output_commands.append(list(command))
        if command[:2] == ["ssh-keygen", "-y"]:
            if self.fail_derivation:
                return ""
            private_key = Path(command[-1])
            identity = self.public_keys.get(private_key, "")
            if identity or not private_key.exists():
                return identity
            for known_path, known_identity in list(self.public_keys.items()):
                if known_path.exists() and private_key.samefile(known_path):
                    self.public_keys[private_key] = known_identity
                    return known_identity
            return ""
        if command[:2] == ["ssh", "-G"]:
            return self.client_config_output
        return ""

    def run(self, command, cwd=None):
        self.commands.append((list(command), cwd))
        if command[:3] != ["ssh-keygen", "-t", "ed25519"] or not self.generate_files:
            return

        private_key = Path(command[command.index("-f") + 1])
        private_key.write_text(PRIVATE_MARKER, encoding="utf-8")
        private_key.with_suffix(".pub").write_text(
            f"{self.generated_public} doubt-managed\n",
            encoding="utf-8",
        )
        self.public_keys[private_key] = self.generated_public


class SshTaskTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = Path(self.workspace.name)
        self.runner = FakeRunner(self.home)

    def tearDown(self):
        self.workspace.cleanup()

    def ssh_directory(self):
        return self.home / ".ssh"

    def private_key(self):
        return self.ssh_directory() / ssh.PRIVATE_KEY_NAME

    def public_key(self):
        return self.ssh_directory() / ssh.PUBLIC_KEY_NAME

    def ssh_config(self):
        return self.ssh_directory() / ssh.SSH_CONFIG_NAME

    def managed_config(self):
        return self.ssh_directory() / ssh.MANAGED_CONFIG_NAME

    def known_hosts(self):
        return self.ssh_directory() / ssh.KNOWN_HOSTS_NAME

    def create_ssh_directory(self, mode=0o700):
        directory = self.ssh_directory()
        directory.mkdir(mode=mode)
        directory.chmod(mode)
        return directory

    def create_private_key(self, identity=PUBLIC_IDENTITY, mode=0o600):
        private_key = self.private_key()
        private_key.write_text(PRIVATE_MARKER, encoding="utf-8")
        private_key.chmod(mode)
        self.runner.public_keys[private_key] = identity
        return private_key

    def create_public_key(self, identity=PUBLIC_IDENTITY, mode=0o644):
        public_key = self.public_key()
        public_key.write_text(f"{identity} doubt-managed\n", encoding="utf-8")
        public_key.chmod(mode)
        return public_key

    def create_valid_pair(self, private_mode=0o600, public_mode=0o644):
        self.create_private_key(mode=private_mode)
        self.create_public_key(mode=public_mode)

    def create_correct_configs(self):
        self.known_hosts().write_text(ssh.GITHUB_KNOWN_HOSTS, encoding="ascii")
        self.known_hosts().chmod(0o600)
        self.managed_config().write_bytes(ssh.MANAGED_CONFIG.encode("utf-8"))
        self.managed_config().chmod(0o600)
        self.ssh_config().write_text(
            f"{ssh.INCLUDE_DIRECTIVE}\n",
            encoding="utf-8",
        )
        self.ssh_config().chmod(0o600)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_missing_ssh_keygen_fails_clearly(self):
        self.runner.available.clear()

        with self.assertRaisesRegex(RuntimeError, "ssh-keygen is required.*openssh"):
            ssh.run(self.runner)

        self.assertFalse(self.ssh_directory().exists())

    def test_missing_ssh_client_fails_clearly(self):
        self.runner.available.remove("ssh")

        with self.assertRaisesRegex(RuntimeError, "ssh is required.*openssh"):
            ssh.run(self.runner)

        self.assertFalse(self.ssh_directory().exists())

    def test_missing_directory_and_keypair_are_created_in_real_mode(self):
        results = ssh.run(self.runner)

        self.assertTrue(self.ssh_directory().is_dir())
        self.assertTrue(self.private_key().is_file())
        self.assertTrue(self.public_key().is_file())
        self.assertEqual(self.mode(self.ssh_directory()), 0o700)
        self.assertEqual(self.mode(self.private_key()), 0o600)
        self.assertEqual(self.mode(self.public_key()), 0o644)
        self.assertEqual(
            [result.status for result in results],
            ["add", "add", "add", "add", "add", "ok", "ok"],
        )

    def test_missing_directory_and_keypair_are_not_created_in_dry_run(self):
        self.runner.dry_run = True

        results = ssh.run(self.runner)

        self.assertFalse(self.ssh_directory().exists())
        self.assertEqual(self.runner.commands, [])
        self.assertEqual(
            [result.name for result in results],
            [
                "create ~/.ssh",
                f"create {ssh.PRIVATE_KEY_NAME}",
                "GitHub host trust",
                f"create {ssh.MANAGED_CONFIG_NAME}",
                f"create {ssh.SSH_CONFIG_NAME} include",
                "synchronize reconciled SSH key with GitHub",
            ],
        )

    def test_incorrect_directory_permissions_are_corrected(self):
        self.create_ssh_directory(0o755)
        self.create_valid_pair()

        ssh.run(self.runner)

        self.assertEqual(self.mode(self.ssh_directory()), 0o700)

    def test_dry_run_does_not_chmod_directory_or_keys(self):
        self.create_ssh_directory(0o755)
        self.create_valid_pair(private_mode=0o644, public_mode=0o600)
        self.runner.dry_run = True

        ssh.run(self.runner)

        self.assertEqual(self.mode(self.ssh_directory()), 0o755)
        self.assertEqual(self.mode(self.private_key()), 0o644)
        self.assertEqual(self.mode(self.public_key()), 0o600)
        self.assertEqual(self.runner.commands, [])

    def test_missing_managed_keypair_is_generated(self):
        self.create_ssh_directory()

        results = ssh.run(self.runner)

        self.assertTrue(self.private_key().exists())
        self.assertTrue(self.public_key().exists())
        self.assertIn(
            f"create {ssh.PRIVATE_KEY_NAME}",
            [item.name for item in results],
        )
        command = self.runner.commands[0][0]
        self.assertEqual(command[:3], ["ssh-keygen", "-t", "ed25519"])
        self.assertEqual(command[command.index("-N") + 1], "")
        self.assertEqual(command[command.index("-C") + 1], "doubt-managed")

    def test_official_host_keys_are_exact_and_idempotent(self):
        self.create_ssh_directory()
        first = ssh.reconcile_known_hosts(self.known_hosts(), False)
        second = ssh.reconcile_known_hosts(self.known_hosts(), False)
        self.assertEqual((first.status, second.status), ("add", "ok"))
        self.assertEqual(self.known_hosts().read_text(encoding="ascii"), ssh.GITHUB_KNOWN_HOSTS)
        self.assertEqual(self.mode(self.known_hosts()), 0o600)

    def test_changed_or_linked_host_keys_fail_closed(self):
        self.create_ssh_directory()
        self.known_hosts().write_text("github.com ssh-ed25519 WRONG\n", encoding="ascii")
        with self.assertRaisesRegex(RuntimeError, "differs from official"):
            ssh.reconcile_known_hosts(self.known_hosts(), False)
        self.known_hosts().unlink()
        self.known_hosts().symlink_to(self.home / "elsewhere")
        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            ssh.reconcile_known_hosts(self.known_hosts(), False)

    def test_dry_run_does_not_generate_missing_keypair(self):
        self.create_ssh_directory()
        self.runner.dry_run = True

        ssh.run(self.runner)

        self.assertFalse(self.private_key().exists())
        self.assertFalse(self.public_key().exists())
        self.assertEqual(self.runner.commands, [])

    def test_existing_valid_keypair_is_preserved(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        private_before = self.private_key().read_bytes()
        public_before = self.public_key().read_bytes()

        results = ssh.run(self.runner)

        self.assertEqual(self.private_key().read_bytes(), private_before)
        self.assertEqual(self.public_key().read_bytes(), public_before)
        self.assertEqual(self.runner.commands, [])
        self.assertEqual(results[0].status, "ok")

    def test_missing_public_key_is_recovered_from_private_key(self):
        self.create_ssh_directory()
        self.create_private_key()
        private_before = self.private_key().read_bytes()

        results = ssh.run(self.runner)

        self.assertEqual(self.private_key().read_bytes(), private_before)
        self.assertEqual(ssh.read_public_key(self.public_key()), PUBLIC_IDENTITY)
        self.assertEqual(self.runner.commands, [])
        self.assertIn(
            f"recover {ssh.PUBLIC_KEY_NAME}",
            [item.name for item in results],
        )

    def test_orphaned_managed_public_key_is_replaced(self):
        self.create_ssh_directory()
        self.create_public_key(OTHER_PUBLIC_IDENTITY)

        results = ssh.run(self.runner)

        self.assertTrue(self.private_key().exists())
        self.assertEqual(ssh.read_public_key(self.public_key()), PUBLIC_IDENTITY)
        self.assertIn(
            f"replace orphaned {ssh.PUBLIC_KEY_NAME}",
            [item.name for item in results],
        )

    def test_invalid_managed_private_key_is_replaced(self):
        self.create_ssh_directory()
        self.private_key().write_text("invalid managed key", encoding="utf-8")
        self.create_public_key(OTHER_PUBLIC_IDENTITY)

        results = ssh.run(self.runner)

        self.assertEqual(self.private_key().read_text(encoding="utf-8"), PRIVATE_MARKER)
        self.assertEqual(ssh.read_public_key(self.public_key()), PUBLIC_IDENTITY)
        self.assertIn(
            f"replace invalid {ssh.PRIVATE_KEY_NAME}",
            [item.name for item in results],
        )

    def test_unrelated_keys_and_arbitrary_files_are_preserved(self):
        directory = self.create_ssh_directory()
        unrelated = {
            "id_ed25519": "personal private key",
            "id_ed25519.pub": "personal public key",
            "id_rsa": "legacy key",
            "work_key": "work key",
            "id_ed25519_doubt_old": "unknown doubt-like key",
            "known_hosts": "example key",
            "authorized_keys": "authorized key",
        }
        for name, contents in unrelated.items():
            (directory / name).write_text(contents, encoding="utf-8")

        ssh.run(self.runner)

        for name, contents in unrelated.items():
            self.assertEqual((directory / name).read_text(encoding="utf-8"), contents)

        self.assertEqual(
            self.ssh_config().read_text(encoding="utf-8"),
            f"{ssh.INCLUDE_DIRECTIVE}\n",
        )

    def test_key_permissions_are_corrected(self):
        self.create_ssh_directory()
        self.create_valid_pair(private_mode=0o644, public_mode=0o600)

        results = ssh.run(self.runner)

        self.assertEqual(self.mode(self.private_key()), 0o600)
        self.assertEqual(self.mode(self.public_key()), 0o644)
        self.assertIn("add", [item.status for item in results])

    def test_mismatched_public_key_is_reconciled_to_private_key(self):
        self.create_ssh_directory()
        self.create_private_key()
        self.create_public_key(OTHER_PUBLIC_IDENTITY)
        private_before = self.private_key().read_bytes()

        results = ssh.run(self.runner)

        self.assertEqual(self.private_key().read_bytes(), private_before)
        self.assertEqual(ssh.read_public_key(self.public_key()), PUBLIC_IDENTITY)
        self.assertIn(
            f"reconcile {ssh.PUBLIC_KEY_NAME}",
            [item.name for item in results],
        )

    def test_post_setup_validation_failure_returns_fail(self):
        self.create_ssh_directory()
        self.runner.fail_derivation = True

        results = ssh.run(self.runner)

        self.assertEqual(results[-1].status, "fail")
        self.assertEqual(results[-1].name, f"validate {ssh.PRIVATE_KEY_NAME}")

    def test_managed_symlink_is_rejected_without_touching_target(self):
        self.create_ssh_directory()
        target = self.home / "unrelated-private-key"
        target.write_text("keep me", encoding="utf-8")
        self.private_key().symlink_to(target)

        with self.assertRaisesRegex(RuntimeError, "must not be a symbolic link"):
            ssh.run(self.runner)

        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")

    def test_private_key_contents_are_never_rendered(self):
        self.create_ssh_directory()
        self.create_valid_pair()

        results = ssh.run(self.runner)
        output = render_report(results, PresentationContext(selected=("ssh",)))

        self.assertNotIn(PRIVATE_MARKER, output)

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is unavailable")
    def test_real_ssh_keygen_creates_and_validates_isolated_keypair(self):
        runner = CommandRunner()

        with redirect_stdout(StringIO()):
            ssh.ensure_ssh_directory(self.ssh_directory(), dry_run=False)
            result = ssh.ensure_managed_keypair(
                self.private_key(),
                self.public_key(),
                runner,
            )

        self.assertEqual(result.status, "add")
        self.assertTrue(
            ssh.valid_keypair(
                self.ssh_directory(),
                self.private_key(),
                self.public_key(),
                runner,
            )
        )

    @unittest.skipUnless(shutil.which("ssh"), "ssh is unavailable")
    def test_real_ssh_expands_managed_config_without_network_access(self):
        runner = CommandRunner()
        self.create_ssh_directory()
        self.managed_config().write_text(ssh.MANAGED_CONFIG, encoding="utf-8")
        self.managed_config().chmod(0o600)

        self.assertTrue(
            ssh.valid_client_config(
                self.managed_config(),
                self.private_key(),
                runner,
            )
        )

    def test_missing_managed_config_is_created_with_mode_0600(self):
        self.create_ssh_directory()
        self.create_valid_pair()

        ssh.run(self.runner)

        self.assertEqual(
            self.managed_config().read_text(encoding="utf-8"),
            ssh.MANAGED_CONFIG,
        )
        self.assertEqual(self.mode(self.managed_config()), 0o600)

    def test_dry_run_does_not_create_client_configuration(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.runner.dry_run = True

        results = ssh.run(self.runner)

        self.assertFalse(self.managed_config().exists())
        self.assertFalse(self.ssh_config().exists())
        result_names = [item.name for item in results]
        self.assertIn(f"create {ssh.MANAGED_CONFIG_NAME}", result_names)
        self.assertIn(f"create {ssh.SSH_CONFIG_NAME} include", result_names)

    def test_dry_run_does_not_update_managed_config_or_mode(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        original = b"Host wrong\n"
        self.managed_config().write_bytes(original)
        self.managed_config().chmod(0o644)
        self.runner.dry_run = True

        ssh.run(self.runner)

        self.assertEqual(self.managed_config().read_bytes(), original)
        self.assertEqual(self.mode(self.managed_config()), 0o644)

    def test_correct_managed_config_is_preserved(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.create_correct_configs()
        before = self.managed_config().read_bytes()

        ssh.run(self.runner)

        self.assertEqual(self.managed_config().read_bytes(), before)

    def test_incorrect_managed_config_is_reconciled(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.managed_config().write_text("Host wrong\n", encoding="utf-8")

        results = ssh.run(self.runner)

        self.assertEqual(
            self.managed_config().read_bytes(),
            ssh.MANAGED_CONFIG.encode(),
        )
        self.assertIn(
            f"update {ssh.MANAGED_CONFIG_NAME}",
            [item.name for item in results],
        )

    def test_managed_config_symlink_is_rejected_without_touching_target(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        target = self.home / "unrelated-config"
        target.write_text("keep me", encoding="utf-8")
        self.managed_config().symlink_to(target)

        with self.assertRaisesRegex(RuntimeError, "must not be a symbolic link"):
            ssh.run(self.runner)

        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")

    def test_main_config_symlink_is_rejected_without_touching_target(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        target = self.home / "unrelated-main-config"
        target.write_text("Host keep\n", encoding="utf-8")
        self.ssh_config().symlink_to(target)

        with self.assertRaisesRegex(RuntimeError, "must not be a symbolic link"):
            ssh.run(self.runner)

        self.assertEqual(target.read_text(encoding="utf-8"), "Host keep\n")

    def test_managed_config_permissions_are_corrected(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.create_correct_configs()
        self.managed_config().chmod(0o644)

        ssh.run(self.runner)

        self.assertEqual(self.mode(self.managed_config()), 0o600)

    def test_missing_main_config_is_created_with_include_and_mode_0600(self):
        self.create_ssh_directory()
        self.create_valid_pair()

        ssh.run(self.runner)

        self.assertEqual(
            self.ssh_config().read_bytes(),
            f"{ssh.INCLUDE_DIRECTIVE}\n".encode(),
        )
        self.assertEqual(self.mode(self.ssh_config()), 0o600)

    def test_existing_user_config_is_preserved_when_include_is_appended(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        original = b"Host work\r\n    HostName work.example\r\n"
        self.ssh_config().write_bytes(original)

        ssh.run(self.runner)

        self.assertEqual(
            self.ssh_config().read_bytes(),
            original + f"{ssh.INCLUDE_DIRECTIVE}\n".encode(),
        )

    def test_exact_managed_include_is_not_duplicated(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.create_correct_configs()
        original = self.ssh_config().read_bytes()

        ssh.run(self.runner)

        self.assertEqual(self.ssh_config().read_bytes(), original)

    def test_duplicate_exact_managed_includes_are_reduced_to_one(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        original = (
            f"{ssh.INCLUDE_DIRECTIVE}\nHost work\n    HostName work.example\n{ssh.INCLUDE_DIRECTIVE}\n"
        ).encode()
        self.ssh_config().write_bytes(original)

        ssh.run(self.runner)

        reconciled = self.ssh_config().read_text(encoding="utf-8")
        self.assertEqual(reconciled.count(ssh.INCLUDE_DIRECTIVE), 1)
        self.assertIn("Host work\n    HostName work.example\n", reconciled)

    def test_unrelated_includes_and_host_blocks_are_preserved(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        original = b"Include ~/.ssh/work_config\nHost internal\n    User deploy\n"
        self.ssh_config().write_bytes(original)

        ssh.run(self.runner)

        self.assertEqual(
            self.ssh_config().read_bytes(),
            original + f"{ssh.INCLUDE_DIRECTIVE}\n".encode(),
        )

    def test_similar_but_non_exact_include_is_preserved(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        original = f"  {ssh.INCLUDE_DIRECTIVE}\n".encode()
        self.ssh_config().write_bytes(original)

        ssh.run(self.runner)

        self.assertEqual(
            self.ssh_config().read_bytes(),
            original + f"{ssh.INCLUDE_DIRECTIVE}\n".encode(),
        )

    def test_dry_run_does_not_modify_existing_main_config_or_modes(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        original = b"Host work\n    User deploy\n"
        self.ssh_config().write_bytes(original)
        self.ssh_config().chmod(0o644)
        self.runner.dry_run = True

        ssh.run(self.runner)

        self.assertEqual(self.ssh_config().read_bytes(), original)
        self.assertEqual(self.mode(self.ssh_config()), 0o644)

    def test_main_config_permissions_are_corrected(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.create_correct_configs()
        self.ssh_config().chmod(0o644)

        ssh.run(self.runner)

        self.assertEqual(self.mode(self.ssh_config()), 0o600)

    def test_invalid_effective_client_config_returns_fail(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.runner.client_config_output = ""

        results = ssh.run(self.runner)

        self.assertEqual(results[-1].status, "fail")

    def test_valid_client_config_ignores_fields_after_required_values(self):
        self.runner.client_config_output = (
            "user git\nhostname github.com\nidentityfile ~/.ssh/doubt\nidentitiesonly yes\ncanonicalizehostname false\n"
        )

        self.assertTrue(
            ssh.valid_client_config(
                self.managed_config(),
                self.private_key(),
                self.runner,
            )
        )

    def test_client_config_requires_git_user_and_managed_identity(self):
        self.create_ssh_directory()
        self.create_valid_pair()
        self.runner.client_config_output = (
            "user root\nhostname github.com\nidentityfile ~/.ssh/id_ed25519\nidentitiesonly no\n"
        )

        results = ssh.run(self.runner)

        self.assertEqual(results[-1].status, "fail")

    def test_validation_is_local_and_does_not_run_agent_or_github_commands(self):
        self.create_ssh_directory()
        self.create_valid_pair()

        results = ssh.run(self.runner)

        self.assertEqual(results[-1].status, "ok")
        self.assertEqual(self.runner.commands, [])
        self.assertTrue(any(command[:2] == ["ssh", "-G"] for command in self.runner.output_commands))
        self.assertFalse(any(command[0] in {"gh", "ssh-add"} for command in self.runner.output_commands))


if __name__ == "__main__":
    unittest.main()
