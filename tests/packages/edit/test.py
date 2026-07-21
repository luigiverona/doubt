from __future__ import annotations

import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from doubt import app
from doubt.cli import main, parse_command
from doubt.core.failure import FailureKind, OperationalError
from doubt.core.plan import PackageRequest
from doubt.packages import edit
from doubt.packages.lists import DesiredState, active_state

ROOT = Path(__file__).resolve().parents[3]


class PackageEditingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "packages"
        shutil.copytree(ROOT / "apps", self.root / "apps")
        shutil.copytree(ROOT / "deps", self.root / "deps")
        self.state = DesiredState(
            self.root,
            self.root / "apps",
            self.root / "deps",
            True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_active_state_distinguishes_source_checkout_and_installed_runtime(self):
        source = active_state(runtime_root=ROOT)
        self.assertFalse(source.installed)
        self.assertEqual(source.root, ROOT)

        runtime = Path(self.temporary.name) / "release"
        runtime.mkdir()
        (runtime / ".doubt-release").write_text("version=1.0.1\n", encoding="utf-8")
        installed = active_state(
            runtime_root=runtime,
            environment={
                "HOME": str(Path(self.temporary.name) / "home"),
                "XDG_CONFIG_HOME": str(Path(self.temporary.name) / "config"),
            },
        )
        self.assertTrue(installed.installed)
        self.assertEqual(installed.root, Path(self.temporary.name) / "config/doubt/packages")
        with self.assertRaisesRegex(OperationalError, "HOME must be an absolute path"):
            active_state(runtime_root=runtime, environment={})
        with self.assertRaisesRegex(OperationalError, "XDG_CONFIG_HOME must be an absolute path"):
            active_state(runtime_root=runtime, environment={"HOME": "/home/test", "XDG_CONFIG_HOME": "relative"})

    def test_list_and_check_are_deterministic_filtered_and_read_only(self):
        before = self.bytes()
        complete = edit.listing(self.state)
        filtered = edit.listing(self.state, "pacman", "browser")
        result = edit.check(self.state)

        self.assertEqual(complete[0].category, "bootstrap")
        self.assertEqual([(item.source, item.category) for item in filtered], [("pacman", "browser")])
        self.assertEqual(result.sources, 3)
        self.assertEqual(result.packages, 17)
        self.assertEqual(self.bytes(), before)
        with self.assertRaisesRegex(OperationalError, "category filter requires"):
            edit.listing(self.state, category="browser")
        with self.assertRaisesRegex(OperationalError, "unknown category"):
            edit.listing(self.state, "pacman", "missing")

    def test_add_existing_new_and_bootstrap_targets_are_sorted_and_safe(self):
        browser = self.state.apps / "pacman/browser"
        result = edit.add(self.state, "pacman", "browser", "firefox")
        self.assertTrue(result.changed)
        self.assertEqual(browser.read_text(encoding="utf-8"), "firefox\ntorbrowser-launcher\n")
        self.assertEqual(stat.S_IMODE(browser.stat().st_mode), 0o644)

        created = edit.add(self.state, "aur", "development", "vesktop-bin")
        self.assertEqual(created.path, self.state.apps / "aur/development")
        self.assertEqual(created.path.read_bytes(), b"vesktop-bin\n")

        bootstrap = edit.add(self.state, "pacman", "bootstrap", "curl")
        self.assertEqual(bootstrap.path, self.state.deps / "pacman/bootstrap")
        self.assertIn("curl\n", bootstrap.path.read_text(encoding="utf-8"))
        self.assertFalse(list(self.root.rglob("*.doubt.*")))

    def test_exact_duplicate_is_noop_and_other_location_is_rejected(self):
        target = self.state.apps / "pacman/browser"
        before = target.read_bytes()
        result = edit.add(self.state, "pacman", "browser", "torbrowser-launcher")
        self.assertFalse(result.changed)
        self.assertEqual(target.read_bytes(), before)

        with self.assertRaisesRegex(OperationalError, "already declared at pacman/browser"):
            edit.add(self.state, "aur", "browser", "torbrowser-launcher")
        self.assertEqual(target.read_bytes(), before)

    def test_dry_run_validates_exact_paths_without_writing(self):
        before = self.bytes()
        added = edit.add(self.state, "flatpak", "image", "org.gimp.GIMP", dry_run=True)
        removed = edit.remove(self.state, "pacman", "mullvad-vpn", dry_run=True)
        self.assertEqual(added.path, self.state.apps / "flatpak/image")
        self.assertEqual(removed.path, self.state.apps / "pacman/vpn")
        self.assertEqual(self.bytes(), before)

    def test_remove_finds_category_cleans_empty_application_and_is_idempotent(self):
        path = self.state.apps / "pacman/vpn"
        result = edit.remove(self.state, "pacman", "mullvad-vpn")
        self.assertEqual(result.category, "vpn")
        self.assertFalse(path.exists())
        missing = edit.remove(self.state, "pacman", "mullvad-vpn")
        self.assertFalse(missing.changed)

    def test_required_bootstrap_structure_and_packages_cannot_be_removed(self):
        with self.assertRaisesRegex(OperationalError, "missing required packages: git"):
            edit.remove(self.state, "pacman", "git")
        self.assertIn("git\n", (self.state.deps / "pacman/bootstrap").read_text(encoding="utf-8"))

    def test_add_remove_round_trip_restores_original_bytes(self):
        target = self.state.apps / "pacman/browser"
        original = target.read_bytes()
        edit.add(self.state, "pacman", "browser", "firefox")
        edit.remove(self.state, "pacman", "firefox")
        self.assertEqual(target.read_bytes(), original)

    def test_add_and_remove_are_consumed_by_the_existing_plan_loader(self):
        edit.add(self.state, "pacman", "browser", "firefox")
        applications, dependencies = app.load_project_lists(self.state.apps, self.state.deps)
        self.assertIn("firefox", {name for item in applications for name in item.apps})
        self.assertEqual(
            {name for item in dependencies for name in item.apps},
            {name for item in edit.load_complete(self.state)[1] for name in item.apps},
        )
        edit.remove(self.state, "pacman", "firefox")
        applications, _ = app.load_project_lists(self.state.apps, self.state.deps)
        self.assertNotIn("firefox", {name for item in applications for name in item.apps})

    def test_invalid_sources_categories_packages_and_combinations_write_nothing(self):
        before = self.bytes()
        cases = (
            ("arch", "browser", "firefox"),
            ("pacman", "../browser", "firefox"),
            ("pacman", "browser", "bad package"),
            ("flatpak", "chat", "not.a"),
            ("aur", "bootstrap", "curl"),
        )
        for source, category, package in cases:
            with self.subTest(source=source, category=category, package=package):
                with self.assertRaises((OperationalError, ValueError)):
                    edit.add(self.state, source, category, package)
        self.assertEqual(self.bytes(), before)

    def test_check_rejects_duplicate_empty_unknown_temporary_and_unsafe_entries(self):
        cases = {
            "duplicate": ("apps/pacman/extra", "mullvad-vpn\n"),
            "empty": ("apps/pacman/empty", "# comment only\n"),
            "unknown": ("apps/unknown/category", "thing\n"),
            "temporary": ("apps/pacman/.browser.swp", "thing\n"),
            "malformed": ("apps/flatpak/image", "bad.flatpak\n"),
        }
        for label, (relative, content) in cases.items():
            with self.subTest(label=label):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                with self.assertRaises((OperationalError, ValueError)):
                    edit.check(self.state)
                if path.is_file():
                    path.unlink()
                if label == "unknown":
                    path.parent.rmdir()

    def test_write_failure_and_candidate_failure_preserve_original(self):
        target = self.state.apps / "pacman/browser"
        original = target.read_bytes()
        failure = OperationalError(FailureKind.ATOMIC_WRITE_FAILURE, "test", "injected")
        with patch("doubt.packages.edit.files.replace_if_unchanged", side_effect=failure):
            with self.assertRaisesRegex(OperationalError, "injected"):
                edit.add(self.state, "pacman", "browser", "firefox")
        self.assertEqual(target.read_bytes(), original)

        real_validate = edit._validate_collections
        validations = 0

        def fail_candidate(*args, **kwargs):
            nonlocal validations
            validations += 1
            if validations == 2:
                raise ValueError("candidate")
            return real_validate(*args, **kwargs)

        with patch("doubt.packages.edit._validate_collections", side_effect=fail_candidate):
            with self.assertRaisesRegex(ValueError, "candidate"):
                edit.add(self.state, "pacman", "browser", "firefox")
        self.assertEqual(target.read_bytes(), original)

    def test_post_write_validation_failure_rolls_back_original(self):
        target = self.state.apps / "pacman/browser"
        original = target.read_bytes()
        real_load = edit.load_complete
        calls = 0

        def failing_load(state: DesiredState):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("post-write")
            return real_load(state)

        with patch("doubt.packages.edit.load_complete", side_effect=failing_load):
            with self.assertRaisesRegex(ValueError, "post-write"):
                edit.add(self.state, "pacman", "browser", "firefox")
        self.assertEqual(target.read_bytes(), original)

        created = self.state.apps / "pacman/newcategory"
        calls = 0
        with patch("doubt.packages.edit.load_complete", side_effect=failing_load):
            with self.assertRaisesRegex(ValueError, "post-write"):
                edit.add(self.state, "pacman", "newcategory", "new-package")
        self.assertFalse(created.exists())

        removed = self.state.apps / "pacman/vpn"
        removed_original = removed.read_bytes()
        calls = 0
        with patch("doubt.packages.edit.load_complete", side_effect=failing_load):
            with self.assertRaisesRegex(ValueError, "post-write"):
                edit.remove(self.state, "pacman", "mullvad-vpn")
        self.assertEqual(removed.read_bytes(), removed_original)

    def test_concurrent_change_is_not_overwritten(self):
        target = self.state.apps / "pacman/browser"
        real_replace = edit.files.replace_if_unchanged

        def concurrent(path: Path, expected: bytes | None, content: bytes, mode: int) -> None:
            path.write_text("externally-changed\n", encoding="utf-8")
            real_replace(path, expected, content, mode)

        with patch("doubt.packages.edit.files.replace_if_unchanged", side_effect=concurrent):
            with self.assertRaisesRegex(OperationalError, "changed concurrently"):
                edit.add(self.state, "pacman", "browser", "firefox")
        self.assertEqual(target.read_text(encoding="utf-8"), "externally-changed\n")

    def test_cli_help_errors_and_execution_do_not_reach_installation_boundaries(self):
        for arguments in (
            ["pkg"],
            ["pkg", "--help"],
            ["pkg", "list", "--help"],
            ["pkg", "add", "--help"],
            ["pkg", "remove", "--help"],
            ["pkg", "check", "--help"],
        ):
            with (
                self.subTest(arguments=arguments),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(main(arguments), 2)
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["pkg", "unknown"]), 2)

        with (
            patch("doubt.app.active_state", return_value=self.state),
            patch("doubt.app.MutationLock.__enter__") as lock,
            patch("doubt.system.run.CommandRunner.run") as run,
            redirect_stdout(StringIO()),
        ):
            for request in (
                PackageRequest("list"),
                PackageRequest("check"),
                PackageRequest("add", "pacman", "browser", "firefox", True),
                PackageRequest("remove", "pacman", package="firefox", dry_run=True),
            ):
                self.assertEqual(app.execute_package(request), 0)
        lock.assert_not_called()
        run.assert_not_called()

    def test_cli_list_check_and_edit_render_the_public_contract(self):
        with patch("doubt.app.active_state", return_value=self.state):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["pkg", "list", "pacman", "browser"]), 0)
            self.assertEqual(output.getvalue(), "pacman\n  browser\n    torbrowser-launcher\n")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["pkg", "check"]), 0)
            self.assertEqual(
                output.getvalue(),
                "Package declarations are valid: 17 packages across 3 sources.\n",
            )

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["pkg", "add", "pacman", "browser", "firefox"]), 0)
            self.assertIn("Added firefox.", output.getvalue())
            self.assertIn("No package was installed.", output.getvalue())

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["pkg", "add", "pacman", "browser", "firefox"]), 0)
            self.assertIn("firefox is already declared", output.getvalue())

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["pkg", "remove", "pacman", "firefox"]), 0)
            self.assertIn("Removed firefox.", output.getvalue())
            self.assertIn("No package was uninstalled.", output.getvalue())

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["pkg", "remove", "pacman", "firefox"]), 0)
            self.assertIn("firefox is not declared", output.getvalue())

    def test_all_existing_workflows_resolve_the_same_effective_paths(self):
        with patch("doubt.cli.active_state", return_value=self.state):
            for arguments in ([], ["plan"], ["verify"]):
                request = parse_command(arguments).request
                self.assertIsNotNone(request)
                self.assertEqual(request.apps, self.state.apps)
                self.assertEqual(request.deps, self.state.deps)

    def test_parser_requires_exact_public_arity_and_limits_dry_run(self):
        for arguments in (
            ["pkg", "add", "pacman", "browser"],
            ["pkg", "remove", "pacman"],
            ["pkg", "check", "extra"],
            ["pkg", "list", "--dry-run"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                parse_command(arguments)

    def bytes(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
