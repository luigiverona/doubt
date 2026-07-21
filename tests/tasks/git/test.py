import tempfile
import unittest
from pathlib import Path

from doubt.system.run import CommandResult
from doubt.tasks import git

VALID_CONFIG = '[git]\nname = "Test User"\nemail = "test@example.com"\ndefault_branch = "main"\n'


class FakeRunner:
    def __init__(self, home, dry_run=False, values=None):
        self.home = Path(home)
        self.dry_run = dry_run
        self.values = dict(values or {})
        self.commands = []
        self.git_available = True
        self.apply_writes = True

    def home_directory(self):
        return self.home

    def command_exists(self, command):
        return command == "git" and self.git_available

    def capture(self, command, env=None):
        self.assert_get_command(command)
        value = self.values.get(command[-1])
        if value is None:
            return CommandResult(1)
        return CommandResult(0, value + "\n")

    def run(self, command, cwd=None):
        self.commands.append(list(command))
        if self.apply_writes:
            self.values[command[3]] = command[4]

    @staticmethod
    def assert_get_command(command):
        if command[:4] != ["git", "config", "--global", "--get"]:
            raise AssertionError(f"unexpected command: {command}")


class ManagedGitTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = Path(self.workspace.name)

    def tearDown(self):
        self.workspace.cleanup()

    def config_path(self):
        return self.home / git.CONFIG_RELATIVE_PATH

    def write_config(self, content=VALID_CONFIG, directory_mode=0o700, file_mode=0o600):
        path = self.config_path()
        path.parent.mkdir(parents=True, mode=directory_mode, exist_ok=True)
        path.parent.chmod(directory_mode)
        path.write_text(content, encoding="utf-8")
        path.chmod(file_mode)
        return path

    def correct_values(self):
        return {
            "user.name": "Test User",
            "user.email": "test@example.com",
            "init.defaultBranch": "main",
        }

    def test_existing_valid_config_is_loaded_without_mutation(self):
        self.write_config()
        runner = FakeRunner(self.home, values=self.correct_values())

        results = git.run(runner, environment={})

        self.assertEqual([result.status for result in results], ["ok", "ok", "ok"])
        self.assertEqual(runner.commands, [])

    def test_environment_initializes_missing_config_and_exact_global_keys(self):
        runner = FakeRunner(self.home)
        unrelated = {"core.editor": "vim"}
        runner.values.update(unrelated)

        results = git.run(
            runner,
            environment={
                "DOUBT_GIT_NAME": "Test User",
                "DOUBT_GIT_EMAIL": "test@example.com",
                "DOUBT_GIT_DEFAULT_BRANCH": "trunk",
            },
        )

        self.assertEqual(git.read_config(self.config_path()).default_branch, "trunk")
        self.assertEqual(self.config_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config_path().parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(runner.values["core.editor"], "vim")
        self.assertEqual(
            [command[3] for command in runner.commands],
            ["user.name", "user.email", "init.defaultBranch"],
        )
        self.assertTrue(all(result.status == "add" for result in results))
        self.assertEqual(list(self.config_path().parent.glob(".config.*")), [])

    def test_existing_global_identity_initializes_missing_config(self):
        runner = FakeRunner(
            self.home,
            values={"user.name": "Existing User", "user.email": "existing@example.com"},
        )

        git.run(runner, environment={})

        settings = git.read_config(self.config_path())
        self.assertEqual(settings.name, "Existing User")
        self.assertEqual(settings.email, "existing@example.com")
        self.assertEqual(settings.default_branch, "main")

    def test_missing_identity_prompts_in_real_mode(self):
        runner = FakeRunner(self.home)
        prompts = []
        answers = iter(["Prompted User", "prompted@example.com"])

        git.run(
            runner,
            environment={},
            input_fn=lambda message: prompts.append(message) or next(answers),
        )

        self.assertEqual(prompts, ["Git name: ", "Git email: "])
        self.assertEqual(git.read_config(self.config_path()).name, "Prompted User")

    def test_prompt_eof_fails_cleanly(self):
        runner = FakeRunner(self.home)

        with self.assertRaisesRegex(RuntimeError, "requires interactive input"):
            git.run(
                runner,
                environment={},
                input_fn=lambda _message: (_ for _ in ()).throw(EOFError()),
            )

    def test_dry_run_missing_identity_warns_without_prompt_or_files(self):
        runner = FakeRunner(self.home, dry_run=True)
        prompts = []

        results = git.run(
            runner,
            environment={},
            input_fn=lambda message: prompts.append(message) or "unexpected",
        )

        self.assertEqual([result.status for result in results], ["warn"])
        self.assertIn("DOUBT_GIT_NAME", results[0].name)
        self.assertEqual(prompts, [])
        self.assertFalse(self.config_path().exists())
        self.assertFalse(self.config_path().parent.exists())
        self.assertEqual(runner.commands, [])

    def test_dry_run_with_environment_plans_without_writes_or_personal_output(self):
        runner = FakeRunner(self.home, dry_run=True)

        results = git.run(
            runner,
            environment={
                "DOUBT_GIT_NAME": "Private Name",
                "DOUBT_GIT_EMAIL": "private@example.com",
            },
        )

        self.assertFalse(self.config_path().exists())
        self.assertEqual(runner.commands, [])
        self.assertTrue(all(result.status == "add" for result in results))
        rendered_names = " ".join(result.name for result in results)
        self.assertNotIn("Private Name", rendered_names)
        self.assertNotIn("private@example.com", rendered_names)

    def test_invalid_toml_and_missing_fields_fail_cleanly(self):
        for content, message in (
            ("[git\n", "invalid"),
            ("[other]\nvalue = 1\n", r"requires a \[git\] section"),
            ('[git]\nname = "Test"\nemail = "test@example.com"\n', "default_branch"),
        ):
            with self.subTest(content=content):
                path = self.write_config(content)
                with self.assertRaisesRegex(RuntimeError, message):
                    git.run(FakeRunner(self.home), environment={})
                path.unlink()

    def test_invalid_identity_values_fail_cleanly(self):
        invalid = (
            ("", "test@example.com", "main"),
            ("bad\nname", "test@example.com", "main"),
            ("Test", "not-an-email", "main"),
            ("Test", "test@example.com", "-main"),
            ("Test", "test@example.com", "bad\nbranch"),
        )
        for name, email, branch in invalid:
            with self.subTest(name=name, email=email, branch=branch):
                with self.assertRaises(RuntimeError):
                    git.validate_settings(name, email, branch)

    def test_invalid_explicit_environment_value_is_not_replaced_by_prompt(self):
        runner = FakeRunner(self.home)
        prompts = []

        with self.assertRaisesRegex(RuntimeError, "Git name is invalid"):
            git.run(
                runner,
                environment={
                    "DOUBT_GIT_NAME": "",
                    "DOUBT_GIT_EMAIL": "test@example.com",
                },
                input_fn=lambda message: prompts.append(message) or "replacement",
            )

        self.assertEqual(prompts, [])

    def test_existing_path_permissions_are_corrected(self):
        path = self.write_config(directory_mode=0o755, file_mode=0o644)
        runner = FakeRunner(self.home, values=self.correct_values())

        results = git.run(runner, environment={})

        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(results[0].status, "add")

    def test_dry_run_does_not_chmod_existing_paths(self):
        path = self.write_config(directory_mode=0o755, file_mode=0o644)

        git.run(FakeRunner(self.home, dry_run=True, values=self.correct_values()), environment={})

        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_unsafe_symlink_and_non_file_paths_fail(self):
        target = self.home / "target"
        target.write_text(VALID_CONFIG, encoding="utf-8")
        path = self.config_path()
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            git.run(FakeRunner(self.home), environment={})
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            git.run(FakeRunner(self.home), environment={})

    def test_unsafe_configuration_directory_fails(self):
        path = self.config_path()
        path.parent.parent.mkdir(parents=True)
        path.parent.write_text("not a directory", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            git.run(FakeRunner(self.home), environment={})

    def test_unrelated_files_are_preserved(self):
        self.write_config()
        unrelated = self.config_path().parent / "notes.txt"
        unrelated.write_text("keep me", encoding="utf-8")

        git.run(FakeRunner(self.home, values=self.correct_values()), environment={})

        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

    def test_only_differing_managed_key_is_updated(self):
        self.write_config()
        for key in self.correct_values():
            with self.subTest(key=key):
                values = self.correct_values()
                values[key] = "different"
                runner = FakeRunner(self.home, values=values)

                results = git.run(runner, environment={})

                self.assertEqual([command[3] for command in runner.commands], [key])
                self.assertEqual(sum(result.status == "add" for result in results), 1)

    def test_multiple_differences_update_only_three_exact_keys(self):
        self.write_config()
        runner = FakeRunner(self.home, values={"alias.co": "checkout"})

        git.run(runner, environment={})

        self.assertEqual(
            [command[3] for command in runner.commands],
            ["user.name", "user.email", "init.defaultBranch"],
        )
        self.assertEqual(runner.values["alias.co"], "checkout")

    def test_post_write_verification_failure_fails(self):
        self.write_config()
        runner = FakeRunner(self.home)
        runner.apply_writes = False

        results = git.run(runner, environment={})

        self.assertTrue(all(result.status == "fail" for result in results))

    def test_second_real_run_is_idempotent(self):
        runner = FakeRunner(self.home)
        environment = {
            "DOUBT_GIT_NAME": "Test User",
            "DOUBT_GIT_EMAIL": "test@example.com",
        }

        first = git.run(runner, environment=environment)
        command_count = len(runner.commands)
        second = git.run(runner, environment={})

        self.assertTrue(any(result.status == "add" for result in first))
        self.assertEqual(len(runner.commands), command_count)
        self.assertTrue(all(result.status == "ok" for result in second))

    def test_missing_git_fails_cleanly(self):
        runner = FakeRunner(self.home)
        runner.git_available = False

        with self.assertRaisesRegex(RuntimeError, "git is required"):
            git.run(runner, environment={})


if __name__ == "__main__":
    unittest.main()
