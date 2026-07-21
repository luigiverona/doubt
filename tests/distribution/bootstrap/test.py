from __future__ import annotations

import hashlib
import subprocess
import unittest
from pathlib import Path

from distribution import archive

ROOT = Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "bootstrap/install"


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.content = INSTALLER.read_text(encoding="utf-8")

    def test_canonical_one_command_is_primary(self):
        command = "curl -fsSL https://doubt.luigiverona.dev/install | bash"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        site = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertEqual(readme.count(command), 1)
        self.assertEqual(site.count(command), 1)
        self.assertLess(readme.index(command), readme.index("## What a run does"))

    def test_installer_requires_bash_strict_mode_arch_and_tty(self):
        for value in (
            'if [[ -z "${BASH_VERSION:-}" ]]',
            "set -euo pipefail",
            "umask 077",
            "-f /etc/arch-release",
            "$(uname -m) == x86_64",
            "exec 3<>/dev/tty",
            "[[ ! -t 3 ]]",
        ):
            self.assertIn(value, self.content)

    def test_no_python_or_manual_dependency_bootstrap(self):
        for forbidden in ("python3", "python -", "pacman -S", "sudo ", "git clone", "pip "):
            self.assertNotIn(forbidden, self.content)

    def test_archive_and_manifest_hashes_match_authoritative_files(self):
        archive_digest = (ROOT / "release/SHA256").read_text(encoding="ascii").split()[0]
        manifest_digest = hashlib.sha256((ROOT / "release/members.txt").read_bytes()).hexdigest()
        self.assertIn(f"readonly ARCHIVE_SHA256='{archive_digest}'", self.content)
        self.assertIn(f"readonly MANIFEST_SHA256='{manifest_digest}'", self.content)
        self.assertIn(f"readonly ARCHIVE_NAME='{archive.ARCHIVE_NAME}'", self.content)
        self.assertIn("/releases/download/1.0.5/", self.content)
        self.assertNotIn("/releases/latest/", self.content)

    def test_long_bootstrap_phases_are_announced_before_their_operations(self):
        pairs = (
            ("Downloading release...", 'download "$ARCHIVE_URL"'),
            ("Downloading manifest...", 'download "$MANIFEST_URL"'),
            ("Verifying release...", 'verify_digest "$ARCHIVE"'),
            ("Extracting release...", 'bsdtar -xpf "$ARCHIVE"'),
            ("Starting Doubt...", '"$STAGE/doubt" "$@"'),
        )
        for phase, operation in pairs:
            self.assertLess(self.content.index(phase), self.content.index(operation))

    def test_all_disposable_state_uses_one_secure_root(self):
        self.assertIn('mktemp -d "$WORK_PARENT/doubt.XXXXXXXX"', self.content)
        self.assertIn('chmod 0700 -- "$WORK"', self.content)
        self.assertIn("trap cleanup EXIT", self.content)
        self.assertIn('"$WORK_PARENT"/doubt.*', self.content)
        self.assertIn("stat -c '%u' -- \"$WORK\"", self.content)
        for value in ("/var/tmp", "$HOME/.cache", "~/.cache", "doubt-bootstrap"):
            self.assertNotIn(value, self.content)

    def test_archive_validation_precedes_extraction_and_execution(self):
        digest = self.content.index('verify_digest "$ARCHIVE"')
        compare = self.content.index('files_match "$EXPECTED_NAMES" "$ACTUAL_NAMES"')
        extract = self.content.index('bsdtar -xpf "$ARCHIVE"')
        marker = self.content.index("printf 'version=%s")
        execute = self.content.index('"$STAGE/doubt" "$@" <&3')
        self.assertLess(digest, compare)
        self.assertLess(compare, extract)
        self.assertLess(extract, marker)
        self.assertLess(marker, execute)

    def test_unsafe_links_types_and_paths_are_rejected(self):
        for value in (
            "release archive contains a link or unsupported file type",
            "release member escapes its root",
            "staged release contains an unsupported member",
            '&& ! -L "$path"',
            '&& ! -L "$STAGE/doubt"',
        ):
            self.assertIn(value, self.content)

    def test_staged_runtime_owns_confirmation_and_persistent_activation(self):
        for value in (
            'DOUBT_BOOTSTRAP_STAGE="$STAGE"',
            'DOUBT_BOOTSTRAP_SHA256="$ARCHIVE_SHA256"',
            'DOUBT_WORK_ROOT="$RUNTIME_WORK"',
        ):
            self.assertIn(value, self.content)
        for forbidden in (".local/bin/doubt", ".local/share/doubt", ".config/doubt"):
            self.assertNotIn(forbidden, self.content)
        self.assertNotIn("Continue?", self.content)

    def test_no_shell_startup_or_desktop_scope(self):
        for forbidden in (".bashrc", ".profile", "fish", "hypr", "caelestia", "wallpaper"):
            self.assertNotIn(forbidden, self.content.lower())

    def test_shell_syntax_is_valid(self):
        result = subprocess.run(["bash", "-n", INSTALLER], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
