import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from doubt.core.failure import FailureKind, OperationalError
from doubt.core.result import InstallItem
from doubt.sources import aur, flatpak, pacman


class FakeRunner:
    def __init__(self, root: Path):
        self.dry_run = False
        self.commands = []
        self.quiet = []
        self.available = set()
        self.installed = set()
        self.install_on_run = set()
        self.remotes = set()
        self.fail_next = False
        self.failure_kind = FailureKind.COMMAND_FAILURE
        (root / "aur").mkdir(exist_ok=True)
        self.environment = {"DOUBT_WORK_ROOT": str(root)}

    def command_exists(self, command):
        return command in self.available

    def succeeds(self, command):
        if command[:2] == ["pacman", "-Qi"]:
            return command[2] in self.installed
        if command[:2] == ["yay", "-Q"]:
            return command[2] in self.installed
        if command[:2] == ["flatpak", "info"]:
            return command[2] in self.installed
        return False

    def output(self, command):
        if command[:2] == ["flatpak", "remotes"]:
            return "\n".join(sorted(self.remotes))
        return ""

    def run(self, command, cwd=None, *, quiet=False):
        self.commands.append((list(command), cwd))
        self.quiet.append(quiet)
        if self.fail_next:
            self.fail_next = False
            raise OperationalError(
                self.failure_kind,
                command[0],
                "injected package failure",
            )
        self.installed.update(self.install_on_run)


class InstallerResultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def runner(self):
        return FakeRunner(self.root)

    def test_pacman_returns_ok_and_add_results(self):
        runner = self.runner()
        runner.available.add("pacman")
        runner.installed.add("git")
        runner.install_on_run.add("base-devel")

        results = pacman.install(
            [
                InstallItem("git", "pacman deps", "bootstrap"),
                InstallItem("base-devel", "pacman deps", "bootstrap"),
            ],
            runner,
            label="pacman dependencies",
        )

        self.assertEqual([result.status for result in results], ["ok", "add"])
        self.assertEqual(
            runner.commands,
            [(["sudo", "pacman", "-S", "--needed", "--", "base-devel"], None)],
        )
        self.assertEqual(runner.quiet, [True])

    def test_aur_returns_ok_and_add_results(self):
        runner = self.runner()
        runner.available.update({"git", "pacman"})
        runner.available.add("yay")
        runner.installed.update({"base-devel", "installed-aur"})
        runner.install_on_run.add("new-aur")

        results = aur.install(
            [
                InstallItem("installed-aur", "aur", "browser"),
                InstallItem("new-aur", "aur", "browser"),
            ],
            runner,
            pacman_deps=("git", "base-devel"),
        )

        self.assertEqual([result.status for result in results], ["ok", "ok", "add"])
        self.assertEqual(
            runner.commands,
            [
                (
                    [
                        "yay",
                        "--builddir",
                        str(Path(runner.environment["DOUBT_WORK_ROOT"]) / "aur"),
                        "-S",
                        "--needed",
                        "--",
                        "new-aur",
                    ],
                    None,
                )
            ],
        )
        self.assertEqual(runner.quiet, [True])

    def test_flatpak_returns_ok_and_add_results(self):
        runner = self.runner()
        runner.available.add("flatpak")
        runner.remotes.add("flathub")
        runner.installed.add("installed.app")
        runner.install_on_run.add("new.app")

        results = flatpak.install(
            [
                InstallItem("installed.app", "flatpak", "chat"),
                InstallItem("new.app", "flatpak", "chat"),
            ],
            runner,
            pacman_deps=("flatpak",),
        )

        self.assertEqual([result.status for result in results], ["ok", "add"])
        self.assertEqual(
            runner.commands,
            [(["flatpak", "install", "--assumeyes", "flathub", "new.app"], None)],
        )
        self.assertEqual(runner.quiet, [True])

    def test_pacman_returns_fail_when_missing_after_successful_batch_install(self):
        runner = self.runner()
        runner.available.add("pacman")

        results = pacman.install(
            [InstallItem("still-missing", "pacman apps", "browser")],
            runner,
            label="pacman apps",
        )

        self.assertEqual([result.status for result in results], ["fail"])

    def test_aur_returns_fail_when_missing_after_successful_batch_install(self):
        runner = self.runner()
        runner.available.update({"git", "pacman", "yay"})
        runner.installed.add("base-devel")

        results = aur.install(
            [InstallItem("still-missing", "aur", "browser")],
            runner,
            pacman_deps=("git", "base-devel"),
        )

        self.assertEqual([result.status for result in results], ["ok", "fail"])

    def test_flatpak_returns_fail_when_missing_after_successful_batch_install(self):
        runner = self.runner()
        runner.available.add("flatpak")
        runner.remotes.add("flathub")

        results = flatpak.install(
            [InstallItem("still.missing", "flatpak", "browser")],
            runner,
            pacman_deps=("flatpak",),
        )

        self.assertEqual([result.status for result in results], ["fail"])

    def test_dry_run_skips_post_install_verification(self):
        runner = self.runner()
        runner.dry_run = True

        results = pacman.install(
            [InstallItem("planned-package", "pacman apps", "browser")],
            runner,
            label="pacman apps",
        )

        self.assertEqual([result.status for result in results], ["add"])

    def test_each_package_source_failure_is_classified_and_retry_is_idempotent(self):
        item = InstallItem("desired", "source", "category")
        cases = []

        pacman_runner = self.runner()
        pacman_runner.available.add("pacman")
        cases.append(
            (
                pacman_runner,
                lambda runner: pacman.install([item], runner, label="pacman applications"),
                FailureKind.PACKAGE_INSTALLATION_FAILURE,
            )
        )

        aur_runner = self.runner()
        aur_runner.available.update({"git", "pacman", "yay"})
        aur_runner.installed.add("base-devel")
        cases.append(
            (
                aur_runner,
                lambda runner: aur.install([item], runner, pacman_deps=("git", "base-devel")),
                FailureKind.PACKAGE_INSTALLATION_FAILURE,
            )
        )

        flatpak_runner = self.runner()
        flatpak_runner.available.add("flatpak")
        flatpak_runner.remotes.add("flathub")
        cases.append(
            (
                flatpak_runner,
                lambda runner: flatpak.install([item], runner, pacman_deps=("flatpak",)),
                FailureKind.FLATPAK_FAILURE,
            )
        )

        for runner, install, expected_kind in cases:
            with self.subTest(kind=expected_kind, available=runner.available):
                runner.install_on_run.add(item.name)
                runner.fail_next = True
                with self.assertRaises(OperationalError) as raised:
                    install(runner)
                self.assertEqual(raised.exception.kind, expected_kind)
                self.assertNotIn(item.name, runner.installed)

                retry = install(runner)
                command_count = len(runner.commands)
                stable = install(runner)
                retry_item = next(result for result in retry if result.name == item.name)
                stable_item = next(result for result in stable if result.name == item.name)
                self.assertEqual(retry_item.status, "add")
                self.assertEqual(stable_item.status, "ok")
                self.assertEqual(len(runner.commands), command_count)

    def test_package_sources_preserve_command_interruption(self):
        item = InstallItem("desired", "source", "category")
        runners = []

        pacman_runner = self.runner()
        pacman_runner.available.add("pacman")
        runners.append((pacman_runner, lambda: pacman.install([item], pacman_runner, label="apps")))

        aur_runner = self.runner()
        aur_runner.available.update({"git", "pacman", "yay"})
        aur_runner.installed.add("base-devel")
        runners.append(
            (
                aur_runner,
                lambda: aur.install([item], aur_runner, pacman_deps=("git", "base-devel")),
            )
        )

        flatpak_runner = self.runner()
        flatpak_runner.available.add("flatpak")
        flatpak_runner.remotes.add("flathub")
        runners.append(
            (
                flatpak_runner,
                lambda: flatpak.install([item], flatpak_runner, pacman_deps=("flatpak",)),
            )
        )

        for runner, install in runners:
            with self.subTest(available=runner.available):
                runner.fail_next = True
                runner.failure_kind = FailureKind.COMMAND_INTERRUPTION
                with self.assertRaises(OperationalError) as raised:
                    install()
                self.assertEqual(raised.exception.kind, FailureKind.COMMAND_INTERRUPTION)

    def test_aur_temporary_workspace_failure_is_classified(self):
        runner = self.runner()
        runner.available.update({"git", "pacman"})
        runner.installed.add("base-devel")
        runner.environment = {}
        with self.assertRaises(OperationalError) as raised:
            aur.ensure_yay(runner)
        self.assertEqual(raised.exception.kind, FailureKind.UNSAFE_PATH)
        self.assertIn("private doubt temporary root", str(raised.exception))

    def test_already_installed_paths_do_not_print_routine_messages(self):
        runner = self.runner()
        runner.available.update({"flatpak", "git", "pacman", "yay"})
        runner.remotes.add("flathub")
        runner.installed.update(
            {
                "base-devel",
                "git",
                "installed-aur",
                "installed-flatpak",
                "installed-pacman",
            }
        )
        stdout = StringIO()

        with redirect_stdout(stdout):
            pacman.install(
                [InstallItem("installed-pacman", "pacman apps", "browser")],
                runner,
                label="pacman apps",
            )
            aur.install(
                [InstallItem("installed-aur", "aur", "browser")],
                runner,
                pacman_deps=("git", "base-devel"),
            )
            flatpak.install(
                [InstallItem("installed-flatpak", "flatpak", "browser")],
                runner,
                pacman_deps=("flatpak",),
            )

        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
