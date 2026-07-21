from __future__ import annotations

import unittest

from doubt.core.failure import FailureKind, OperationalError


class FailureTaxonomyTests(unittest.TestCase):
    def test_taxonomy_covers_recovery_boundaries(self):
        expected = {
            "blocked precondition",
            "unavailable executable",
            "command failure",
            "command interruption",
            "malformed command output",
            "malformed package metadata",
            "malformed TOML",
            "malformed JSON state",
            "unsafe path",
            "unsafe symlink",
            "file type mismatch",
            "directory type mismatch",
            "permission denial",
            "ownership mismatch",
            "atomic-write failure",
            "package metadata failure",
            "remote metadata unavailable",
            "package conflict safety failure",
            "package installation failure",
            "Flatpak failure",
            "concurrent mutation already running",
            "invalid desired state",
            "concurrent desired-state change",
        }
        self.assertEqual({kind.value for kind in FailureKind}, expected)

    def test_operational_error_retains_component(self):
        error = OperationalError(
            FailureKind.COMMAND_FAILURE,
            "github",
            "GitHub authentication failed",
        )
        self.assertEqual(str(error), "GitHub authentication failed")
        self.assertEqual(error.component, "github")


if __name__ == "__main__":
    unittest.main()
