import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubt import app
from doubt.core.plan import parse, resolve
from doubt.core.result import InstallResult
from doubt.core.task import TASK_ORDER
from doubt.packages import resolve as conflicts
from doubt.packages.lists import PackageList, load_lists
from doubt.system.run import CommandResult, CommandRunner
from doubt.tasks import codex
from doubt.ui.render import PresentationContext, render_report, selected_sections


class FakeRunner:
    def __init__(self, home: Path, *, dry_run: bool = False):
        self.home = home
        self.dry_run = dry_run
        self.status = {"01": False, "02": False}
        self.calls = []
        self.complete_login = True
        self.failed_logins = set()

    def home_directory(self):
        return self.home

    def capture(self, command, env=None):
        self.calls.append(("capture", list(command), dict(env or {})))
        if command[:2] == ["pacman", "-Qi"]:
            return CommandResult(0)
        profile = self._profile(env)
        if command[-2:] == ["login", "status"]:
            return CommandResult(0 if self.status[profile] else 1)
        return CommandResult(1)

    def run(self, command, cwd=None, env=None, **_kwargs):
        self.calls.append(("run", list(command), dict(env or {})))
        profile = self._profile(env)
        if self.complete_login and profile not in self.failed_logins:
            self.status[profile] = True

    @staticmethod
    def _profile(env):
        value = (env or {})["CODEX_HOME"]
        return "01" if value.endswith(".codex-01") else "02"


class CodexTaskTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = Path(self.workspace.name)
        self.binary = self.home / "bin" / "codex"
        self.binary.parent.mkdir()
        self.binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.environment = {"PATH": str(self.home / ".local" / "bin")}

    def tearDown(self):
        self.workspace.cleanup()

    def test_dependencies_are_declared_once_in_deterministic_group(self):
        dependencies = load_lists(Path("deps"))
        group = next(item for item in dependencies if item.category == "codex")

        self.assertEqual(group.apps, ("openai-codex", "nodejs", "ripgrep"))
        all_names = [name for item in dependencies for name in item.apps]
        self.assertEqual(all_names.count("git"), 1)

    def test_launcher_names_change_without_changing_profile_homes(self):
        self.assertEqual(
            [(profile.launcher, profile.relative_home, profile.legacy_launcher) for profile in codex.PROFILES],
            [
                ("codex-01", Path(".codex-01"), "codex-personal"),
                ("codex-02", Path(".codex-02"), "codex-work"),
            ],
        )

    def test_task_order_places_codex_before_verification(self):
        self.assertEqual(TASK_ORDER[-3:], ("codex", "path", "verify"))
        self.assertEqual(resolve(parse("codex"), None), ("codex",))

    def test_command_runner_passes_codex_home_without_echoing_it(self):
        with patch("doubt.system.run.subprocess.run") as run:
            CommandRunner().run(["/usr/bin/codex", "login"], env={"CODEX_HOME": "/test/home"})

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["CODEX_HOME"], "/test/home")

    def test_codex_inventory_selects_only_codex_dependencies(self):
        inventory = conflicts.build_inventory([], load_lists(Path("deps")), ("codex",))

        self.assertEqual(
            tuple(item.name for item in inventory.native),
            ("openai-codex", "nodejs", "ripgrep"),
        )

    def test_selected_codex_closure_contains_packages_dependencies_and_codex(self):
        self.assertEqual(
            selected_sections(("codex",)),
            ("dependencies", "applications", "setup", "verification"),
        )

    def test_selective_codex_run_installs_only_codex_dependency_group(self):
        dependencies = (
            PackageList("pacman", "bootstrap", ("git",), Path("deps/pacman/bootstrap")),
            PackageList("pacman", "codex", codex.REQUIRED_PACKAGES, Path("deps/pacman/codex")),
        )
        runner = FakeRunner(self.home, dry_run=True)
        captured = []

        def install(items, _runner, label):
            captured.extend(item.name for item in items)
            return [InstallResult(item.name, item.source, item.category, "add") for item in items]

        with (
            patch("doubt.app.package_preflight.preflight", return_value=([], True)),
            patch("doubt.sources.pacman.install", side_effect=install),
            patch("doubt.app.codex.run", return_value=[]),
        ):
            app.run_installers([], dependencies, runner, ("codex",))

        self.assertEqual(tuple(captured), codex.REQUIRED_PACKAGES)

    def test_failed_codex_dependency_prevents_configuration_and_login(self):
        dependencies = (PackageList("pacman", "codex", codex.REQUIRED_PACKAGES, Path("deps/pacman/codex")),)
        runner = FakeRunner(self.home)
        failed = [InstallResult("openai-codex", "pacman deps", "codex", "fail")]

        with (
            patch("doubt.app.package_preflight.preflight", return_value=([], True)),
            patch("doubt.sources.pacman.install", return_value=failed),
            patch("doubt.app.codex.run") as setup,
        ):
            results = app.run_installers([], dependencies, runner, ("codex",))

        setup.assert_not_called()
        self.assertEqual(results, failed)

    def test_presentation_shows_unambiguous_accounts_and_verification_group(self):
        dependencies = (PackageList("pacman", "codex", codex.REQUIRED_PACKAGES, Path("deps/pacman/codex")),)
        results = [
            *(InstallResult(name, "pacman deps", "codex", "ok") for name in codex.REQUIRED_PACKAGES),
            InstallResult("01 home", "codex", "codex", "ok"),
            InstallResult("01 storage", "codex", "codex", "ok"),
            InstallResult("01 account", "codex", "codex", "ok"),
            InstallResult("01 launcher", "codex", "codex", "ok"),
            InstallResult("02 home", "codex", "codex", "ok"),
            InstallResult("02 storage", "codex", "codex", "ok"),
            InstallResult("02 account", "codex", "codex", "ok"),
            InstallResult("02 launcher", "codex", "codex", "ok"),
            InstallResult("Codex dual-account setup", "verify", "verify", "ok"),
        ]

        output = render_report(
            results,
            PresentationContext(("codex", "verify"), dependency_lists=dependencies),
        )

        self.assertEqual(output, "Verification passed.\nWorkstation ready.")
        self.assertNotIn("auth.json", output)

    def test_dry_run_counts_each_codex_operation_once(self):
        dependencies = (PackageList("pacman", "codex", codex.REQUIRED_PACKAGES, Path("deps/pacman/codex")),)
        results = [
            *(InstallResult(name, "pacman deps", "codex", "add") for name in codex.REQUIRED_PACKAGES),
            InstallResult("create 01 home", "codex", "codex", "add"),
            InstallResult("configure 01 storage", "codex", "codex", "add"),
            InstallResult("01 authentication requires interactive login", "codex", "codex", "warn"),
            InstallResult("create 01 launcher", "codex", "codex", "add"),
            InstallResult("create 02 home", "codex", "codex", "add"),
            InstallResult("configure 02 storage", "codex", "codex", "add"),
            InstallResult("02 authentication requires interactive login", "codex", "codex", "warn"),
            InstallResult("create 02 launcher", "codex", "codex", "add"),
        ]

        output = render_report(
            results,
            PresentationContext(("codex",), dependency_lists=dependencies, planning=True),
        )

        self.assertIn("Dependencies", output)
        self.assertIn("Codex profiles", output)
        self.assertIn("Plan complete. No changes were made.", output)

    def test_real_run_creates_distinct_homes_configs_and_launchers(self):
        runner = FakeRunner(self.home)
        default_home = self.home / ".codex"
        default_home.mkdir()
        default_marker = default_home / "existing-state"
        default_marker.write_text("preserve", encoding="utf-8")

        results = codex.run(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )

        for profile in codex.PROFILES:
            profile_home = self.home / profile.relative_home
            config = profile_home / codex.CONFIG_NAME
            launcher = self.home / ".local" / "bin" / profile.launcher
            self.assertTrue(profile_home.is_dir())
            self.assertEqual(mode(profile_home), 0o700)
            self.assertEqual(config.read_text(encoding="utf-8"), 'cli_auth_credentials_store = "file"\n')
            self.assertEqual(mode(config), 0o600)
            self.assertEqual(launcher.read_text(encoding="utf-8"), codex.launcher_content(profile, self.binary))
            self.assertEqual(mode(launcher), 0o755)
        self.assertEqual(default_marker.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(
            [call[2]["CODEX_HOME"] for call in runner.calls if call[0] == "run"],
            [
                str(self.home / ".codex-01"),
                str(self.home / ".codex-02"),
            ],
        )
        self.assertTrue(any(result.name == "authenticate 01 account" for result in results))

    def test_existing_authenticated_profiles_are_idempotent(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)
        runner.calls.clear()

        results = codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)

        self.assertFalse(any(result.status == "add" for result in results))
        self.assertFalse(any(call[0] == "run" for call in runner.calls))

    def test_legacy_profiles_are_atomically_migrated_with_unknown_files(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        preserved = {}
        for profile in codex.PROFILES:
            legacy = self.home / profile.legacy_home
            legacy.mkdir(mode=0o700)
            (legacy / "nested").mkdir()
            payload = f"opaque-{profile.label}".encode()
            preserved[profile.label] = payload
            (legacy / "nested" / "unknown.bin").write_bytes(payload)
            config = legacy / codex.CONFIG_NAME
            config.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
            config.chmod(0o600)
            auth = legacy / codex.AUTH_NAME
            auth.write_bytes(b"REDACTED AUTH FIXTURE")
            auth.chmod(0o600)

        results = codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)

        self.assertEqual(
            [item.name for item in results if item.name.startswith("migrate ")],
            ["migrate Codex 01 profile", "migrate Codex 02 profile"],
        )
        for profile in codex.PROFILES:
            self.assertFalse((self.home / profile.legacy_home).exists())
            numeric = self.home / profile.relative_home
            self.assertEqual((numeric / "nested" / "unknown.bin").read_bytes(), preserved[profile.label])
            self.assertEqual((numeric / codex.AUTH_NAME).read_bytes(), b"REDACTED AUTH FIXTURE")

    def test_old_and_new_profile_collision_fails_without_changes(self):
        profile = codex.PROFILES[0]
        old = self.home / profile.legacy_home
        new = self.home / profile.relative_home
        old.mkdir()
        new.mkdir()
        with self.assertRaisesRegex(RuntimeError, "migration conflict"):
            codex.run(FakeRunner(self.home), home=self.home, binary=self.binary)
        self.assertTrue(old.is_dir())
        self.assertTrue(new.is_dir())

    def test_legacy_launchers_are_renamed_without_reauthentication_or_profile_changes(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        launcher_directory = self.home / ".local" / "bin"
        launcher_directory.mkdir(parents=True)
        auth_bytes = {}
        for profile in codex.PROFILES:
            profile_home = self.home / profile.relative_home
            profile_home.mkdir(mode=0o700)
            config = profile_home / codex.CONFIG_NAME
            config.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
            config.chmod(0o600)
            auth = profile_home / codex.AUTH_NAME
            auth_bytes[profile.label] = f"PRESERVED {profile.label} AUTH".encode()
            auth.write_bytes(auth_bytes[profile.label])
            auth.chmod(0o600)
            legacy = launcher_directory / str(profile.legacy_launcher)
            legacy.write_text(
                codex.old_launcher_content(profile, self.binary, str(profile.legacy_launcher)),
                encoding="utf-8",
            )
            legacy.chmod(0o755)

        results = codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)

        self.assertEqual(
            [item.name for item in results if item.name.startswith("rename ")],
            ["rename 01 launcher", "rename 02 launcher"],
        )
        self.assertFalse(any(call[0] == "run" for call in runner.calls))
        for profile in codex.PROFILES:
            self.assertFalse((launcher_directory / str(profile.legacy_launcher)).exists())
            launcher = launcher_directory / profile.launcher
            self.assertEqual(launcher.read_text(encoding="utf-8"), codex.launcher_content(profile, self.binary))
            self.assertEqual(
                (self.home / profile.relative_home / codex.AUTH_NAME).read_bytes(),
                auth_bytes[profile.label],
            )

    def test_modified_legacy_launcher_blocks_migration_without_deletion_or_new_launcher(self):
        profile = codex.PROFILES[0]
        launcher_directory = self.home / ".local" / "bin"
        launcher_directory.mkdir(parents=True)
        legacy = launcher_directory / str(profile.legacy_launcher)
        legacy.write_text("user managed\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "unmanaged launcher blocks codex-personal"):
            codex.reconcile_launcher(profile, launcher_directory, self.binary, False)

        self.assertEqual(legacy.read_text(encoding="utf-8"), "user managed\n")
        self.assertFalse((launcher_directory / profile.launcher).exists())

    def test_missing_work_authentication_invokes_only_work_login(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": False}

        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)

        login_homes = [call[2]["CODEX_HOME"] for call in runner.calls if call[0] == "run"]
        self.assertEqual(login_homes, [str(self.home / ".codex-02")])

    def test_partial_authentication_retries_only_incomplete_profile(self):
        runner = FakeRunner(self.home)
        runner.failed_logins.add("02")

        first = codex.run(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )
        self.assertTrue(runner.status["01"])
        self.assertFalse(runner.status["02"])
        self.assertTrue(any(item.status == "fail" and "02 authentication" in item.name for item in first))

        runner.failed_logins.clear()
        runner.calls.clear()
        second = codex.run(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )
        login_homes = [call[2]["CODEX_HOME"] for call in runner.calls if call[0] == "run"]
        self.assertEqual(login_homes, [str(self.home / ".codex-02")])
        self.assertFalse(any(item.status == "fail" for item in second))

    def test_dry_run_never_creates_state_or_starts_login(self):
        runner = FakeRunner(self.home, dry_run=True)

        results = codex.run(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )

        self.assertFalse((self.home / ".codex-01").exists())
        self.assertFalse((self.home / ".local").exists())
        self.assertFalse(any(call[0] == "run" for call in runner.calls))
        self.assertEqual(
            [item.status for item in results if "authentication requires" in item.name],
            ["warn", "warn"],
        )

    def test_missing_cli_blocks_real_configuration_before_filesystem_mutation(self):
        missing = self.home / "missing-codex"

        results = codex.run(
            FakeRunner(self.home),
            home=self.home,
            environment=self.environment,
            binary=missing,
        )

        self.assertEqual([item.status for item in results], ["fail"])
        self.assertFalse((self.home / ".codex-01").exists())
        self.assertFalse((self.home / ".local").exists())

    def test_existing_configuration_preserves_unrelated_content_and_comment(self):
        profile_home = self.home / ".codex-personal"
        profile_home.mkdir(mode=0o700)
        config = profile_home / "config.toml"
        config.write_text(
            '# retained\ncli_auth_credentials_store = "keyring" # retained inline\nmodel = "gpt-test"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)

        codex.reconcile_profile_home(codex.PROFILES[0], profile_home, False)

        content = config.read_text(encoding="utf-8")
        self.assertIn("# retained\n", content)
        self.assertIn("# retained inline", content)
        self.assertIn('model = "gpt-test"', content)
        self.assertIn('cli_auth_credentials_store = "file"', content)

    def test_malformed_configuration_blocks_without_overwrite(self):
        profile_home = self.home / ".codex-personal"
        profile_home.mkdir(mode=0o700)
        config = profile_home / "config.toml"
        original = "not = [valid\n"
        config.write_text(original, encoding="utf-8")
        config.chmod(0o600)

        with self.assertRaisesRegex(RuntimeError, "invalid config.toml"):
            codex.reconcile_profile_home(codex.PROFILES[0], profile_home, False)
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_unsafe_symlink_home_blocks_without_touching_destination(self):
        destination = self.home / "destination"
        destination.mkdir()
        (self.home / ".codex-personal").symlink_to(destination, target_is_directory=True)

        with self.assertRaisesRegex(RuntimeError, "unsafe legacy Codex 01 home"):
            codex.run(
                FakeRunner(self.home),
                home=self.home,
                environment=self.environment,
                binary=self.binary,
            )
        self.assertEqual(list(destination.iterdir()), [])

    def test_conflicting_launcher_blocks_without_overwrite(self):
        launcher_dir = self.home / ".local" / "bin"
        launcher_dir.mkdir(parents=True)
        launcher = launcher_dir / "codex-01"
        launcher.write_text("user managed\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "unmanaged launcher"):
            codex.run(
                FakeRunner(self.home),
                home=self.home,
                environment=self.environment,
                binary=self.binary,
            )
        self.assertEqual(launcher.read_text(encoding="utf-8"), "user managed\n")

    def test_verification_rejects_a_remaining_legacy_launcher(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)
        profile = codex.PROFILES[0]
        legacy = self.home / ".local" / "bin" / str(profile.legacy_launcher)
        legacy.write_text(
            codex.old_launcher_content(profile, self.binary, str(profile.legacy_launcher)),
            encoding="utf-8",
        )
        legacy.chmod(0o755)

        results = codex.verify_state(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )

        self.assertTrue(any(item.status == "fail" and "legacy 01 launcher" in item.name for item in results))

    def test_verification_reports_legacy_numeric_profile_collision_without_mutation(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)
        legacy = self.home / ".codex-personal"
        legacy.mkdir(mode=0o700)
        before = tuple(self.home.rglob("*"))

        results = codex.verify_state(runner, home=self.home, binary=self.binary)

        self.assertTrue(any(item.status == "fail" and "migration conflict" in item.name for item in results))
        self.assertEqual(tuple(self.home.rglob("*")), before)

    def test_authentication_post_status_is_required(self):
        runner = FakeRunner(self.home)
        runner.complete_login = False

        results = codex.run(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )

        self.assertEqual(
            [item.status for item in results if "authentication failed" in item.name],
            ["fail", "fail"],
        )

    def test_auth_file_permissions_are_repaired_without_reading_content(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)
        auth = self.home / ".codex-01" / "auth.json"
        auth.write_bytes(b"REDACTED TEST DATA")
        auth.chmod(0o644)

        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)

        self.assertEqual(mode(auth), 0o600)
        self.assertEqual(auth.read_bytes(), b"REDACTED TEST DATA")

    def test_launcher_forwards_arguments_working_directory_environment_and_status(self):
        profile = codex.PROFILES[0]
        fake = self.home / "fake-codex"
        fake.write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$CODEX_HOME" "$PWD" "$1" "$2"\nexit 23\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        launcher = self.home / profile.launcher
        launcher.write_text(codex.launcher_content(profile, fake), encoding="utf-8")
        launcher.chmod(0o755)
        self.assertTrue(launcher.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\nset -euo pipefail\n"))
        syntax = subprocess.run(
            ["bash", "-n", str(launcher)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        working = self.home / "working"
        working.mkdir()

        completed = subprocess.run(
            [str(launcher), "one argument", "two"],
            cwd=working,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 23)
        self.assertEqual(
            completed.stdout.splitlines(),
            [str(self.home / ".codex-01"), str(working), "one argument", "two"],
        )

    def test_verification_is_read_only_and_checks_both_status_environments(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)
        runner.calls.clear()

        results = codex.verify_state(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )

        self.assertEqual([item.status for item in results], ["ok"])
        self.assertFalse(any(call[0] == "run" for call in runner.calls))
        self.assertEqual(
            [call[2]["CODEX_HOME"] for call in runner.calls if "CODEX_HOME" in call[2]],
            [str(self.home / ".codex-01"), str(self.home / ".codex-02")],
        )

    def test_verification_reports_auth_mode_without_repairing(self):
        runner = FakeRunner(self.home)
        runner.status = {"01": True, "02": True}
        codex.run(runner, home=self.home, environment=self.environment, binary=self.binary)
        auth = self.home / ".codex-02" / "auth.json"
        auth.write_text("REDACTED TEST DATA", encoding="utf-8")
        auth.chmod(0o644)

        results = codex.verify_state(
            runner,
            home=self.home,
            environment=self.environment,
            binary=self.binary,
        )

        self.assertTrue(any(item.status == "fail" and "permissions" in item.name for item in results))
        self.assertEqual(mode(auth), 0o644)

    def test_missing_path_does_not_block_setup_or_modify_shell_files(self):
        runner = FakeRunner(self.home, dry_run=True)

        results = codex.run(runner, home=self.home, environment={"PATH": "/usr/bin"}, binary=self.binary)

        self.assertFalse(any("PATH" in item.name for item in results))
        self.assertFalse((self.home / ".config" / "fish").exists())

    def test_unmanaged_system_binary_is_reported_without_login_or_state_creation(self):
        runner = FakeRunner(self.home, dry_run=True)
        with (
            patch.object(codex, "CODEX_BINARY", self.binary),
            patch.object(codex, "official_binary", return_value=False),
        ):
            results = codex.run(
                runner,
                home=self.home,
                environment=self.environment,
                binary=self.binary,
            )

        self.assertTrue(any(item.status == "warn" and "not owned" in item.name for item in results))
        self.assertFalse(any(call[0] == "run" for call in runner.calls))
        self.assertFalse((self.home / ".codex-01").exists())

    def test_package_ownership_requires_exact_arch_package(self):
        runner = FakeRunner(self.home)
        with patch.object(
            runner,
            "capture",
            return_value=CommandResult(
                0,
                "/usr/bin/codex is owned by openai-codex-unofficial 1-1\n",
            ),
        ):
            self.assertFalse(codex.official_binary(runner, Path("/usr/bin/codex")))

    def test_managed_home_validation_rejects_root_relative_and_missing_paths(self):
        for path in (Path("/"), Path("relative"), self.home / "missing"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                codex.validate_base_home(path)
        codex.validate_base_home(self.home)

    def test_managed_profile_homes_must_be_distinct(self):
        path = self.home / ".codex-personal"
        with self.assertRaisesRegex(RuntimeError, "must be distinct"):
            codex.validate_distinct_homes((path, path))

    def test_multiline_storage_setting_is_rejected_without_rewrite(self):
        content = 'cli_auth_credentials_store = """unsafe"""\n'
        with self.assertRaisesRegex(RuntimeError, "invalid multiline"):
            codex.update_config(content)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


if __name__ == "__main__":
    unittest.main()
