import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubt.core.failure import OperationalError
from doubt.packages.lists import active_state, group_by_source, load_lists, parse_app_file


class ListParsingTests(unittest.TestCase):
    def test_parse_app_file_ignores_blank_lines_and_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser"
            path.write_text(
                """
# browsers
firefox

librewolf-bin # inline comment
""",
                encoding="utf-8",
            )

            self.assertEqual(parse_app_file(path), ("firefox", "librewolf-bin"))

    def test_load_lists_groups_by_source(self):
        with tempfile.TemporaryDirectory() as directory:
            apps = Path(directory) / "apps"
            (apps / "pacman").mkdir(parents=True)
            (apps / "flatpak").mkdir(parents=True)
            (apps / "pacman" / "browser").write_text("firefox\n", encoding="utf-8")
            (apps / "flatpak" / "mail").write_text(
                "com.tutanota.Tutanota\n",
                encoding="utf-8",
            )

            grouped = group_by_source(load_lists(apps))

            self.assertEqual(grouped["pacman"], ["firefox"])
            self.assertEqual(grouped["aur"], [])
            self.assertEqual(grouped["flatpak"], ["com.tutanota.Tutanota"])

    def test_load_dependency_lists_groups_by_source(self):
        with tempfile.TemporaryDirectory() as directory:
            deps = Path(directory) / "deps"
            (deps / "pacman").mkdir(parents=True)
            (deps / "pacman" / "bootstrap").write_text(
                """
# bootstrap deps
git

base-devel
flatpak # inline comment
""",
                encoding="utf-8",
            )

            grouped = group_by_source(load_lists(deps))

            self.assertEqual(grouped["pacman"], ["git", "base-devel", "flatpak"])
            self.assertEqual(grouped["aur"], [])
            self.assertEqual(grouped["flatpak"], [])

    def test_repository_does_not_keep_dependencies_under_apps(self):
        repo_root = Path(__file__).resolve().parents[3]

        self.assertFalse((repo_root / "apps" / "pacman" / "deps").exists())
        self.assertTrue((repo_root / "deps" / "pacman" / "bootstrap").exists())

    def test_rejects_whitespace_inside_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser"
            path.write_text("bad package\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                parse_app_file(path)

    def test_disappearing_file_and_invalid_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("doubt.packages.lists.files.capture", return_value=None):
                with self.assertRaisesRegex(ValueError, "disappeared concurrently"):
                    parse_app_file(root / "missing")
            with self.assertRaisesRegex(ValueError, "root is missing"):
                load_lists(root / "missing", require_root=True)
            self.assertEqual(load_lists(root / "missing"), [])
            file_root = root / "file"
            file_root.write_text("value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a directory"):
                load_lists(file_root)

    def test_invalid_sources_entries_modes_and_categories_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stray = root / "stray"
            stray.write_text("value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source entries"):
                load_lists(root)
            stray.unlink()
            (root / "unknown").mkdir()
            with self.assertRaisesRegex(ValueError, "unknown source"):
                load_lists(root)

        cases = ("source-file", "category-directory", "bad-mode", "bad-category")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "pacman"
                if case == "source-file":
                    source.write_text("value\n", encoding="utf-8")
                else:
                    source.mkdir()
                    category = source / ("Bad-Category" if case == "bad-category" else "browser")
                    if case == "category-directory":
                        category.mkdir()
                    else:
                        category.write_text("firefox\n", encoding="utf-8")
                        if case == "bad-mode":
                            category.chmod(0o600)
                with self.assertRaises(ValueError):
                    load_lists(root)

    def test_installed_state_requires_safe_absolute_paths_and_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".doubt-release").write_text("release\n", encoding="utf-8")
            with self.assertRaises(OperationalError):
                active_state(runtime_root=root, environment={})
            with self.assertRaises(OperationalError):
                active_state(runtime_root=root, environment={"HOME": str(root), "XDG_CONFIG_HOME": "relative"})
            with self.assertRaisesRegex(OperationalError, "not installed"):
                active_state(
                    runtime_root=root,
                    environment={"HOME": str(root)},
                    require_materialized=True,
                )
            materialized = root / ".config/doubt/packages"
            materialized.mkdir(parents=True)
            state = active_state(runtime_root=root, environment={"HOME": str(root)})
            self.assertEqual(state.root, materialized)

    def test_symlinked_allowed_source_is_rejected_at_source_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "lists"
            root.mkdir()
            target = base / "target"
            target.mkdir()
            (root / "pacman").symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "package source"):
                load_lists(root)
