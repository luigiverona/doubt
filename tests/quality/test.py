from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality import actions, artifacts, coverage, danger, imports, naming, secrets, terms


class QualityGateTests(unittest.TestCase):
    def scan(self, content: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.py"
            path.write_text(content, encoding="utf-8")
            return subprocess.run(
                ["detect-secrets-hook", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_repository_naming_actions_terms_and_danger_gates_are_clean(self):
        self.assertEqual(naming.violations(), [])
        self.assertEqual(actions.violations(), [])
        self.assertEqual(terms.violations(), [])
        self.assertEqual(danger.subprocess_violations(), [])
        self.assertEqual(danger.shell_violations(), [])

    def test_required_workflows_have_distinct_validation_responsibilities(self):
        root = Path(__file__).resolve().parents[2]
        workflows = root / ".github" / "workflows"
        ci = (workflows / "ci.yml").read_text(encoding="utf-8")
        security = (workflows / "security.yml").read_text(encoding="utf-8")
        acceptance = (workflows / "acceptance.yml").read_text(encoding="utf-8")
        container = (root / "acceptance" / "container" / "check").read_text(encoding="utf-8")

        for gate in ("unit", "structure", "typing", "coverage", "distribution"):
            self.assertIn(f"./check {gate}", ci)
        for gate in ("secrets", "danger", "actions", "advisory"):
            self.assertIn(f"./check {gate}", security)
            self.assertNotIn(f"./check {gate}", ci)
        for duplicated in ("structure", "distribution", "shell"):
            self.assertNotIn(f"./check {duplicated}", security)
        self.assertIn("./acceptance/container/check", acceptance)
        self.assertNotIn("requirements/dev.txt", acceptance)
        self.assertNotIn("./check", container)

    def test_import_catalog_contains_every_production_module(self):
        names = imports.module_names()
        self.assertIn("doubt.cli", names)
        self.assertIn("doubt.packages.resolve", names)
        self.assertIn("doubt.system.run", names)
        self.assertIn("doubt.tasks.github.keys", names)

    def test_artifact_gate_detects_tool_caches_coverage_and_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "doubt" / "__pycache__").mkdir(parents=True)
            (root / ".pytest_cache").mkdir()
            (root / "htmlcov").mkdir()
            (root / "generated.pyc").write_bytes(b"bytecode")
            (root / "generated.pyo").write_bytes(b"optimized bytecode")
            (root / "coverage.xml").write_text("generated", encoding="utf-8")
            (root / "doubt-1.0.0.tar.gz").write_bytes(b"generated archive")
            with patch.object(artifacts, "ROOT", root):
                self.assertEqual(
                    artifacts.violations(),
                    [
                        ".pytest_cache",
                        "coverage.xml",
                        "doubt/__pycache__",
                        "doubt-1.0.0.tar.gz",
                        "generated.pyc",
                        "generated.pyo",
                        "htmlcov",
                    ],
                )

    def test_secret_gate_detects_high_confidence_tokens_without_rendering_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "credential.txt"
            path.write_text("ghp_" + "a" * 24, encoding="utf-8")
            with patch.object(secrets, "ROOT", root):
                found = secrets.violations()
            self.assertEqual(found, ["credential.txt:1: possible GitHub token"])
            self.assertNotIn("ghp_", found[0])

    def test_recognized_secret_scanner_accepts_only_the_inline_allowlisted_action_sha(self):
        action_sha = actions.PINNED_ACTIONS["actions/checkout"][0]
        allowed = self.scan(f'PIN = "{action_sha}"  # pragma: allowlist secret\n')
        candidate = self.scan(f'PIN = "{action_sha}"\n')

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertNotEqual(candidate.returncode, 0)
        self.assertIn("Hex High Entropy String", candidate.stdout)

    def test_recognized_secret_scanner_still_rejects_token_and_unrelated_hex_candidates(self):
        token_value = "ghp_" + "ABCDefghIJklMNop" + "QRstUVwxYZ0123456789"
        token = self.scan(f'TOKEN = "{token_value}"\n')
        hex_value = "0123456789abcdef" * 2 + "01234567"
        unrelated = self.scan(f'VALUE = "{hex_value}"\n')

        self.assertNotEqual(token.returncode, 0)
        self.assertIn("High Entropy String", token.stdout)
        self.assertNotEqual(unrelated.returncode, 0)
        self.assertIn("Hex High Entropy String", unrelated.stdout)

    def test_secret_gate_has_no_broad_scanner_exclusion(self):
        script = (Path(__file__).resolve().parents[2] / "check").read_text(encoding="utf-8")
        self.assertIn('git -c safe.directory="$ROOT" ls-files -z > "$WORK/tracked"', script)
        self.assertIn("xargs -0 detect-secrets-hook", script)
        self.assertIn('git -c safe.directory="$ROOT" diff --check', script)
        self.assertIn("--exclude-lines 'PRIVATE_KEY_(NAME|MODE)'", script)
        for forbidden in ("--baseline", "--exclude-files", ".secrets.baseline"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)

    def test_coverage_percentages_handle_normal_and_empty_totals(self):
        self.assertEqual(coverage.percentage(0, 0), 100.0)
        self.assertEqual(coverage.percentage(9, 10), 90.0)
        summary: coverage.Summary = {
            "covered_lines": 9,
            "num_statements": 10,
            "covered_branches": 4,
            "num_branches": 5,
        }
        self.assertAlmostEqual(coverage.combined(summary), 100 * 13 / 15)


if __name__ == "__main__":
    unittest.main()
