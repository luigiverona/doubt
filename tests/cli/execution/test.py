from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from doubt.app import execute, execute_package, run_installers
from doubt.core.failure import FailureKind, OperationalError
from doubt.core.plan import Mode, PackageRequest, Request
from doubt.core.result import InstallResult
from doubt.packages.lists import PackageList
from doubt.system.activation import ActivationPlan


class FakeRunner:
    def __init__(self, home: Path):
        self.home = home
        self.dry_run = False
        self.details = False
        self.environment: dict[str, str] = {}
        self.available_commands: set[str] = set()

    def home_directory(self) -> Path:
        return self.home


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.runner = FakeRunner(self.home)
        self.request = Request(mode=Mode.MUTATE)
        self.loader = lambda _load: ([], [])

    def capture(self, request: Request, **patches):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = execute(request, self.runner, self.loader, patches.pop("confirm", None))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_plan_is_read_only_and_never_confirms_or_activates(self):
        confirm = Mock()
        result = [InstallResult("git", "pacman deps", "bootstrap", "add")]
        with (
            patch("doubt.app.activation.inspect", return_value=ActivationPlan(None, False, False, False)),
            patch("doubt.app.activation.apply") as activate,
            patch("doubt.app.run_installers", return_value=result) as run,
        ):
            status, output, error = self.capture(Request(mode=Mode.PLAN), confirm=confirm)
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Dependencies", output)
        self.assertIn("No changes were made", output)
        self.assertTrue(run.call_args.kwargs["planning"])
        activate.assert_not_called()
        confirm.assert_not_called()

    def test_decline_leaves_activation_and_mutation_untouched(self):
        result = [InstallResult("git", "pacman deps", "bootstrap", "add")]
        with (
            patch("doubt.app.activation.inspect", return_value=ActivationPlan(None, False, True, False)),
            patch("doubt.app.activation.apply") as activate,
            patch("doubt.app.run_installers", return_value=result) as run,
            patch("doubt.app.MutationLock") as lock,
        ):
            status, output, error = self.capture(self.request, confirm=lambda: False)
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Declined. No changes were made.", output)
        self.assertEqual(run.call_count, 1)
        activate.assert_not_called()
        lock.assert_not_called()

    def test_confirmed_run_activates_then_reconciles_and_verifies(self):
        planned = [InstallResult("git", "pacman deps", "bootstrap", "add")]
        completed = [
            InstallResult("git", "pacman deps", "bootstrap", "add"),
            InstallResult("managed state", "verify", "verify", "ok"),
        ]

        def run_side_effect(*_args, **kwargs):
            if kwargs["planning"]:
                return planned
            progress = kwargs["progress"]
            progress("Installing dependencies", "start")
            progress("Installing dependencies", "done")
            kwargs["results"].extend(completed)
            return kwargs["results"]

        initial = ActivationPlan(None, False, True, False)
        applied = ActivationPlan(None, False, False, False)
        state = SimpleNamespace(apps=Path("apps"), deps=Path("deps"))
        with (
            patch("doubt.app.activation.inspect", return_value=initial),
            patch("doubt.app.activation.apply", return_value=applied) as activate,
            patch("doubt.app.run_installers", side_effect=run_side_effect) as run,
            patch("doubt.app.active_state", return_value=state),
            patch("doubt.app.load_project_lists", return_value=([], [])),
            patch("doubt.app.MutationLock"),
        ):
            status, output, error = self.capture(self.request, confirm=lambda: True)
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(run.call_count, 2)
        self.assertIn("verify", run.call_args.args[3])
        activate.assert_called_once_with(initial)
        self.assertIn("Installing dependencies\ndone\n\nComplete", output)
        self.assertIn("Complete", output)
        self.assertIn("Failed         0", output)

    def test_normal_workflow_blank_lines_and_column_zero_rows_are_stable(self):
        planned = [InstallResult("git", "pacman deps", "bootstrap", "add")]
        completed = [
            InstallResult("git", "pacman deps", "bootstrap", "add"),
            InstallResult("managed state", "verify", "verify", "ok"),
        ]

        def run_side_effect(*_args, **kwargs):
            if kwargs["planning"]:
                return planned
            progress = kwargs["progress"]
            for label in (
                "Installing dependencies",
                "Installing applications",
                "Configuring workstation",
                "Verifying workstation",
            ):
                progress(label, "start")
                progress(label, "done")
            kwargs["results"].extend(completed)
            return kwargs["results"]

        def confirm() -> bool:
            print("Continue? [y/N] y")
            return True

        initial = ActivationPlan(None, False, True, False)
        state = SimpleNamespace(apps=Path("apps"), deps=Path("deps"))
        with (
            patch("doubt.app.activation.inspect", return_value=initial),
            patch("doubt.app.activation.apply", return_value=ActivationPlan(None, False, False, False)),
            patch("doubt.app.run_installers", side_effect=run_side_effect),
            patch("doubt.app.active_state", return_value=state),
            patch("doubt.app.load_project_lists", return_value=([], [])),
            patch("doubt.app.MutationLock"),
        ):
            status, output, error = self.capture(self.request, confirm=confirm)

        self.assertEqual((status, error), (0, ""))
        self.assertEqual(output.count("Continue? [y/N]"), 1)
        self.assertIn("Dependencies", output)
        self.assertIn("Installing dependencies", output)
        self.assertIn("Verifying workstation", output)
        self.assertIn("Failed         0", output)
        self.assertTrue(output.endswith("Workstation ready.\n"))
        self.assertLess(len(output.encode("utf-8")), 4096)

    def test_no_change_run_verifies_without_confirmation(self):
        confirm = Mock()
        unchanged = [InstallResult("git", "pacman deps", "bootstrap", "ok")]
        verified = [InstallResult("managed state", "verify", "verify", "ok")]
        with (
            patch("doubt.app.activation.inspect", return_value=ActivationPlan(None, False, False, False)),
            patch("doubt.app.run_installers", return_value=unchanged),
            patch("doubt.app.verify.run", return_value=verified) as verification,
        ):
            status, output, error = self.capture(self.request, confirm=confirm)
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(
            output,
            "Doubt 1.0.5\n\nNo changes required.\n\nVerifying workstation...\n"
            "Verification passed.\nWorkstation ready.\n",
        )
        confirm.assert_not_called()
        verification.assert_called_once()

    def test_verify_is_read_only_noninteractive_and_strict(self):
        confirm = Mock()
        failures = [InstallResult("missing git", "verify", "verify", "fail")]
        with patch("doubt.app.run_installers", return_value=failures) as run:
            status, output, error = self.capture(Request(mode=Mode.VERIFY), confirm=confirm)
        self.assertEqual((status, error), (1, ""))
        self.assertIn("Verification failed", output)
        self.assertFalse(run.call_args.kwargs["planning"])
        self.assertEqual(run.call_args.args[3], ("verify",))
        confirm.assert_not_called()

    def test_preflight_failure_stops_before_confirmation(self):
        confirm = Mock()
        failure = [InstallResult("package conflict", "packages", "preflight", "fail")]
        with (
            patch("doubt.app.activation.inspect", return_value=ActivationPlan(None, False, False, False)),
            patch("doubt.app.run_installers", return_value=failure),
            patch("doubt.app.activation.apply") as activate,
        ):
            status, output, _error = self.capture(self.request, confirm=confirm)
        self.assertEqual(status, 1)
        self.assertIn("Preflight blocked", output)
        confirm.assert_not_called()
        activate.assert_not_called()

    def test_expected_operational_failure_is_concise(self):
        failure = OperationalError(FailureKind.UNSAFE_PATH, "temporary workspace", "unsafe root")
        with patch("doubt.app.WorkRoot", side_effect=failure):
            status, output, error = self.capture(self.request)
        self.assertEqual((status, output), (1, ""))
        self.assertEqual(error, "invalid: unsafe root\n")

    def test_unexpected_validation_failure_is_concise(self):
        with patch("doubt.app.WorkRoot", side_effect=ValueError("invalid input")):
            status, output, error = self.capture(self.request)
        self.assertEqual((status, output), (1, ""))
        self.assertEqual(error, "invalid: invalid input\n")

    def test_no_change_verification_failure_is_reported(self):
        unchanged = [InstallResult("git", "pacman deps", "bootstrap", "ok")]
        failed = [InstallResult("missing git", "verify", "verify", "fail")]
        with (
            patch("doubt.app.activation.inspect", return_value=ActivationPlan(None, False, False, False)),
            patch("doubt.app.run_installers", return_value=unchanged),
            patch("doubt.app.verify.run", return_value=failed),
        ):
            status, output, error = self.capture(self.request)
        self.assertEqual((status, error), (1, ""))
        self.assertIn("Verification failed", output)

    def test_post_confirmation_operational_failures_are_verified_when_possible(self):
        planned = [InstallResult("git", "pacman deps", "bootstrap", "add")]
        failure = OperationalError(FailureKind.COMMAND_FAILURE, "pacman", "injected")
        initial = ActivationPlan(Path("stage"), True, True, True)
        with (
            patch("doubt.app.activation.inspect", return_value=initial),
            patch("doubt.app.activation.apply", side_effect=failure),
            patch("doubt.app.run_installers", return_value=planned),
            patch("doubt.app.MutationLock"),
        ):
            status, output, error = self.capture(self.request, confirm=lambda: True)
        self.assertEqual((status, error), (1, ""))
        self.assertIn("injected", output)

        state = SimpleNamespace(apps=Path("apps"), deps=Path("deps"))
        calls = 0

        def installers(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return planned
            raise failure

        verification_failure = OperationalError(FailureKind.COMMAND_FAILURE, "verify", "verify injected")
        with (
            patch("doubt.app.activation.inspect", return_value=initial),
            patch("doubt.app.activation.apply", return_value=ActivationPlan(None, False, False, False)),
            patch("doubt.app.run_installers", side_effect=installers),
            patch("doubt.app.active_state", return_value=state),
            patch("doubt.app.load_project_lists", return_value=([], [])),
            patch("doubt.app.verify.run", side_effect=verification_failure) as verification,
            patch("doubt.app.MutationLock"),
        ):
            status, output, error = self.capture(self.request, confirm=lambda: True)
        self.assertEqual((status, error), (1, ""))
        verification.assert_called_once()
        self.assertIn("injected", output)

    def test_internal_orchestration_covers_each_group_without_public_selectors(self):
        dependency = InstallResult("codex", "pacman deps", "codex", "add")
        events: list[tuple[str, str]] = []
        with (
            patch("doubt.app.package_preflight.preflight", return_value=([], True)),
            patch("doubt.app.deps.run", return_value=[dependency]) as dependency_run,
            patch("doubt.app.apps.run", return_value=[InstallResult("app", "flatpak", "apps", "add")]),
            patch("doubt.app.github.run", return_value=[]),
            patch("doubt.app.ssh.run", return_value=[]),
            patch("doubt.app.git_task.run", return_value=[]),
            patch("doubt.app.codex.run", return_value=[]) as codex_run,
            patch("doubt.app.verify.run", return_value=[]),
        ):
            results = run_installers(
                [],
                [],
                self.runner,
                ("deps", "apps", "github", "ssh", "git", "codex", "verify"),
                planning=True,
                progress=lambda label, state: events.append((label, state)),
            )
        self.assertIn(dependency, results)
        dependency_run.assert_called_once_with([], self.runner)
        codex_run.assert_called_once_with(self.runner)
        self.assertIn(("Installing dependencies", "start"), events)
        self.assertIn(("Configuring GitHub", "unchanged"), events)
        self.assertIn(("Configuring Codex 01", "unchanged"), events)
        self.assertEqual(results[-1].name, "final verification after planned changes")

    def test_orchestration_stops_on_conflict_and_skips_codex_after_dependency_failure(self):
        conflict = InstallResult("conflict", "packages", "preflight", "fail")
        with (
            patch("doubt.app.package_preflight.preflight", return_value=([conflict], False)),
            patch("doubt.app.deps.run") as dependency_run,
        ):
            self.assertEqual(run_installers([], [], self.runner, ("deps",)), [conflict])
        dependency_run.assert_not_called()

        failed = InstallResult("codex", "pacman deps", "codex", "fail")
        with (
            patch("doubt.app.package_preflight.preflight", return_value=([], True)),
            patch("doubt.app.deps.run", return_value=[failed]) as dependency_run,
            patch("doubt.app.codex.run") as codex_run,
        ):
            results = run_installers([], [], self.runner, ("codex",), progress=None)
        self.assertEqual(results, [failed])
        self.assertEqual(dependency_run.call_args.kwargs["category"], "codex")
        codex_run.assert_not_called()

    def test_failed_provider_stage_never_emits_done(self):
        failure = OperationalError(FailureKind.COMMAND_FAILURE, "flatpak", "provider failed")
        events: list[tuple[str, str]] = []
        with (
            patch("doubt.app.package_preflight.preflight", return_value=([], True)),
            patch("doubt.app.apps.run", side_effect=failure),
        ):
            with self.assertRaises(OperationalError):
                run_installers(
                    [],
                    [],
                    self.runner,
                    ("apps",),
                    planning=False,
                    progress=lambda label, state: events.append((label, state)),
                )
        self.assertEqual(events, [("Installing applications", "start")])

    def test_interrupted_provider_stage_never_emits_done(self):
        events: list[tuple[str, str]] = []
        with (
            patch("doubt.app.package_preflight.preflight", return_value=([], True)),
            patch("doubt.app.apps.run", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_installers(
                    [],
                    [],
                    self.runner,
                    ("apps",),
                    planning=False,
                    progress=lambda label, state: events.append((label, state)),
                )
        self.assertEqual(events, [("Installing applications", "start")])

    def test_package_dispatch_defensive_and_validation_failures(self):
        state = SimpleNamespace(installed=True)
        with patch("doubt.app.active_state", return_value=state):
            for request in (
                PackageRequest("add"),
                PackageRequest("remove"),
                PackageRequest("unknown"),
            ):
                with self.subTest(action=request.action), self.assertRaises(RuntimeError):
                    execute_package(request)
        failure = OperationalError(FailureKind.INVALID_DESIRED_STATE, "packages", "invalid")
        stderr = StringIO()
        with patch("doubt.app.active_state", side_effect=failure), redirect_stderr(stderr):
            self.assertEqual(execute_package(PackageRequest("check")), 1)
        self.assertIn("invalid", stderr.getvalue())

    def test_default_loader_and_runner_capability_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apps_dir, deps_dir = root / "apps", root / "deps"
            apps_dir.mkdir()
            deps_dir.mkdir()
            request = Request(mode=Mode.PLAN, apps=apps_dir, deps=deps_dir)

            class MinimalRunner:
                dry_run = False

            with (
                patch("doubt.app.activation.inspect", return_value=ActivationPlan(None, False, False, False)),
                patch("doubt.app.run_installers", return_value=[]),
            ):
                status, _output, error = self.capture(request)
                self.assertEqual((status, error), (0, ""))
                status = execute(request, MinimalRunner())  # type: ignore[arg-type]
                self.assertEqual(status, 0)

        openssh = PackageList("pacman", "ssh", ("openssh",), Path("deps/pacman/ssh"))
        with (
            patch("doubt.app.activation.inspect", return_value=ActivationPlan(None, False, False, False)),
            patch("doubt.app.run_installers", return_value=[]),
        ):
            execute(Request(mode=Mode.PLAN), self.runner, lambda _load: ([], [openssh]))
        self.assertIn("ssh-keygen", self.runner.available_commands)

    def test_verify_only_internal_orchestration_skips_mutating_preflight(self):
        with (
            patch("doubt.app.package_preflight.preflight") as preflight,
            patch("doubt.app.verify.run", return_value=[]) as verification,
        ):
            self.assertEqual(run_installers([], [], self.runner, ("verify",), planning=False), [])
        preflight.assert_not_called()
        verification.assert_called_once_with([], [], self.runner, warn_only=False)


if __name__ == "__main__":
    unittest.main()
