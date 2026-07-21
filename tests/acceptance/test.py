from __future__ import annotations

import unittest
from pathlib import Path

from quality.actions import ARCH_IMAGE


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.checker = Path("acceptance/container/check").read_text(encoding="utf-8")
        self.runner = Path("acceptance/container/run").read_text(encoding="utf-8")

    def test_candidate_runs_the_exact_piped_shell_shape_with_a_tty(self):
        self.assertIn(
            'script -qefc "curl -fsSL http://127.0.0.1:$PORT/install | bash"',
            self.checker,
        )
        self.assertIn("runuser -u doubtaccept", self.checker)
        self.assertIn("printf '%s\\n' \"$answer\"", self.checker)
        self.assertIn("Continue? [y/N]", self.checker)

    def test_target_has_no_python_before_installer_execution(self):
        removal = self.checker.index("pacman " + "-Rdd --noconfirm python python-pip")
        invocation = self.checker.index("run_installer \"$DECLINE_HOME\"")
        self.assertLess(removal, invocation)
        self.assertIn("! command -v python3", self.checker)
        self.assertIn("python prerequisite: absent", self.checker)

    def test_decline_first_run_and_idempotent_rerun_are_all_checked(self):
        for value in (
            "Declined. No changes were made.",
            "Installing dependencies",
            "Installing applications",
            "Configuring GitHub",
            "Configuring Codex 01",
            "Configuring launcher PATH",
            "Verifying workstation",
            "Workstation ready.",
            "Looking for matches?",
            "--verbose",
            "verbose provider output: streamed",
            "preserved acceptance declaration",
            "mutations_before",
            "auth_before",
            "codex-01",
            "codex-02",
        ):
            self.assertIn(value, self.checker)

    def test_residue_and_host_fingerprints_are_checked(self):
        for value in (
            "before_packages",
            "after_packages",
            "before_repository",
            "after_repository",
            "FIRST_TMP",
            "SECOND_TMP",
            "THIRD_TMP",
            ".cache/doubt",
            "*makepkg*",
        ):
            self.assertIn(value, self.checker)

    def test_fixture_models_all_external_boundaries_without_shell_bypass(self):
        fixture = Path("acceptance/fixtures/tool").read_text(encoding="utf-8")
        for command in (
            "pacman)",
            "yay)",
            "flatpak)",
            "sudo)",
            "curl)",
            "git)",
            "gh)",
            "ssh-keygen)",
            "ssh)",
            "codex)",
            "vercmp)",
        ):
            self.assertIn(command, fixture)
        self.assertNotIn("eval ", fixture)

    def test_containerfile_uses_the_audited_immutable_arch_image(self):
        containerfile = Path("acceptance/Containerfile").read_text(encoding="utf-8")
        self.assertEqual(containerfile.splitlines()[0], f"FROM {ARCH_IMAGE}")
        self.assertIn("requirements/build.txt", containerfile)
        self.assertNotIn("requirements/dev.txt", containerfile)

    def test_namespace_fallback_is_host_isolated_and_pinned(self):
        for value in (
            "--tmpfs /tmp",
            "--tmpfs /run",
            '--ro-bind "$ROOT" /workspace',
            "archlinux-bootstrap-2026.07.01-x86_64.tar.zst",
            "BOOTSTRAP_SHA256=",
            "archive.archlinux.org/repos/2026/07/12",
        ):
            self.assertIn(value, self.runner)

    def test_repository_owned_scripts_have_one_bash_contract(self):
        scripts = (
            "acceptance/container/check",
            "acceptance/container/run",
            "acceptance/fixtures/tool",
            "bootstrap/install",
            "check",
            "install",
            "release/build",
            "release/publish",
        )
        for name in scripts:
            with self.subTest(script=name):
                content = Path(name).read_text(encoding="utf-8")
                self.assertEqual(content.splitlines()[0], "#!/usr/bin/env bash")
                self.assertIn("set -euo pipefail", content)


if __name__ == "__main__":
    unittest.main()
