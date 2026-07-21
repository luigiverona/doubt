from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from doubt.core.result import InstallResult, PackageCheckResult, PackageEditResult
from doubt.ui.render import (
    PresentationContext,
    product_heading,
    render_final,
    render_package_check,
    render_package_edit,
    render_plan,
    render_verification,
)


class RendererTests(unittest.TestCase):
    def context(self, **values) -> PresentationContext:
        return PresentationContext(selected=values.pop("selected", ("deps", "apps", "verify")), **values)

    def test_plan_snapshot_is_concise_and_grouped(self):
        results = [
            InstallResult("git", "pacman deps", "bootstrap", "add"),
            InstallResult("firefox", "pacman apps", "browser", "add"),
            InstallResult("github authentication", "github", "auth", "add"),
        ]
        self.assertEqual(
            render_plan(results, self.context(activation_required=2)),
            "Doubt 1.0.5\n\nChecking workstation...\n\n"
            "Dependencies          1 required\n"
            "Applications          1 required\n"
            "GitHub and SSH        0 required\n"
            "Git configuration     0 required\n"
            "Codex profiles        0 required\n"
            "Doubt activation      2 required\n"
            "Launcher setup        0 required\n"
            "PATH integration      0 required",
        )

    def test_read_only_plan_names_no_mutation(self):
        result = [InstallResult("git", "pacman deps", "bootstrap", "add")]
        output = render_plan(result, self.context(planning=True))
        self.assertTrue(output.endswith("Plan complete. No changes were made."))

    def test_no_change_snapshot_is_minimal(self):
        result = [InstallResult("git", "pacman deps", "bootstrap", "ok")]
        self.assertEqual(render_plan(result, self.context()), "Doubt 1.0.5\n\nNo changes required.")

    def test_preflight_failure_is_actionable_and_hides_internals(self):
        output = render_plan(
            [InstallResult("visual-studio-code-bin conflicts with code", "packages", "preflight", "fail")],
            self.context(),
        )
        self.assertIn("Preflight blocked", output)
        self.assertIn("visual-studio-code-bin conflicts with code", output)
        self.assertIn("run doubt again", output)
        self.assertNotIn("adapter", output)

    def test_success_snapshot_has_one_count_block(self):
        results = [
            InstallResult("git", "pacman deps", "bootstrap", "add"),
            InstallResult("spotify", "flatpak", "music", "add"),
            InstallResult("ssh", "ssh", "setup", "add"),
            InstallResult("github", "github", "auth", "ok"),
            InstallResult("state", "verify", "verify", "ok"),
        ]
        self.assertEqual(
            render_final(results, self.context(activation_required=2)),
            "Complete\n\nInstalled      2\nConfigured     3\nUnchanged      1\nFailed         0\n\nWorkstation ready.",
        )

    def test_partial_failure_does_not_claim_rollback(self):
        output = render_final(
            [
                InstallResult("git", "pacman deps", "bootstrap", "add"),
                InstallResult("flatpak command failed", "flatpak", "chat", "fail"),
            ],
            self.context(),
        )
        self.assertIn("Workstation setup incomplete", output)
        self.assertIn("Installed      1", output)
        self.assertIn("Failed         1", output)
        self.assertIn("flatpak command failed", output)
        self.assertNotIn("rollback", output.lower())

    def test_operational_failure_includes_exact_reason(self):
        output = render_final([], self.context(), operational_error="pacman failed with exit code 1")
        self.assertIn("Workstation setup failed", output)
        self.assertIn("pacman failed with exit code 1", output)
        self.assertIn("run doubt again", output)

    def test_successful_verification_is_concise(self):
        result = [InstallResult("state", "verify", "verify", "ok")]
        self.assertEqual(render_verification(result), "Doubt 1.0.5\n\nVerification passed.\nWorkstation ready.")

    def test_heading_uses_the_canonical_version_binding(self):
        self.assertEqual(product_heading(), "Doubt 1.0.5")
        with patch("doubt.ui.render.VERSION", "9.8.7"):
            self.assertEqual(product_heading(), "Doubt 9.8.7")

    def test_failed_verification_has_one_supported_recovery_command(self):
        result = [InstallResult("missing package git", "verify", "verify", "fail")]
        output = render_verification(result)
        self.assertIn("Verification failed", output)
        self.assertIn("missing package git", output)
        self.assertIn("Run `doubt`", output)
        for removed in ("--only", "--except", "tasks"):
            self.assertNotIn(removed, output)

    def test_normal_and_verbose_verification_show_every_failure(self):
        results = [
            InstallResult("first failure", "verify", "verify", "fail"),
            InstallResult("second failure", "verify", "verify", "fail"),
        ]
        ordinary = render_verification(results)
        verbose = render_verification(results, details=True)
        self.assertIn("second failure", ordinary)
        self.assertIn("second failure", verbose)

    def test_renderer_has_no_terminal_escape_or_unicode_dependency(self):
        output = render_final([], self.context())
        self.assertNotIn("\x1b", output)
        output.encode("ascii")

    def test_package_check_is_one_line(self):
        result = PackageCheckResult(Path("packages"), sources=3, packages=17)
        self.assertEqual(
            render_package_check(result),
            "Package declarations are valid: 17 packages across 3 sources.",
        )

    def test_package_edit_states_that_no_package_operation_occurred(self):
        added = PackageEditResult("add", "pacman", "firefox", Path("packages"), changed=True)
        removed = PackageEditResult("remove", "pacman", "firefox", Path("packages"), changed=True)
        self.assertIn("No package was installed", render_package_edit(added, installed=True))
        self.assertIn("No package was uninstalled", render_package_edit(removed, installed=True))


if __name__ == "__main__":
    unittest.main()
