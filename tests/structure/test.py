from __future__ import annotations

import ast
import importlib.util
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "doubt"
TESTS = ROOT / "tests"
DUNDERS = {"__init__", "__main__"}
FORBIDDEN = {"utils", "helpers", "common", "misc", "shared", "manager", "service", "base"}
LAYERS = {
    "core": {"core"},
    "system": {"core", "system"},
    "packages": {"core", "system", "packages"},
    "sources": {"core", "system", "packages", "sources"},
    "tasks": {"core", "system", "packages", "sources", "tasks"},
    "ui": {"core", "ui"},
    "app": {"core", "system", "packages", "sources", "tasks", "ui", "app"},
    "cli": {"core", "system", "packages", "sources", "tasks", "ui", "app", "cli"},
}
MUTATIONS = {"chmod", "mkdir", "unlink", "write_text", "write_bytes", "replace"}


def modules(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports(path: Path) -> set[str]:
    current = name(path)
    package = current if path.stem == "__init__" else current.rpartition(".")[0]
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            if module:
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


class StructureTests(unittest.TestCase):
    def test_repository_names_are_one_word(self):
        for root in (SOURCE, TESTS):
            for path in sorted(root.rglob("*")):
                if "__pycache__" in path.parts:
                    continue
                relative = path.relative_to(root)
                if path.is_dir():
                    self.assertNotRegex(path.name, r"[_-]", str(relative))
                elif path.suffix == ".py" and path.stem not in DUNDERS:
                    self.assertNotRegex(path.stem, r"[_-]", str(relative))

    def test_ordinary_tests_are_named_test(self):
        for path in modules(TESTS):
            if path.stem not in DUNDERS:
                self.assertEqual(path.name, "test.py", str(path.relative_to(ROOT)))

    def test_forbidden_catchall_modules_do_not_exist(self):
        for path in modules(SOURCE):
            self.assertNotIn(path.stem, FORBIDDEN, str(path.relative_to(ROOT)))

    def test_layer_import_direction(self):
        for path in modules(SOURCE):
            relative = path.relative_to(SOURCE)
            owner = relative.parts[0] if len(relative.parts) > 1 else relative.stem
            if owner not in LAYERS:
                continue
            for imported in imports(path):
                if not imported.startswith("doubt."):
                    continue
                target = imported.split(".")[1]
                self.assertIn(
                    target,
                    LAYERS[owner],
                    f"{relative} imports higher layer {imported}",
                )

    def test_production_never_imports_tests(self):
        for path in modules(SOURCE):
            self.assertFalse(
                any(item == "tests" or item.startswith("tests.") for item in imports(path)),
                str(path.relative_to(ROOT)),
            )

    def test_runtime_never_imports_distribution_or_site_tooling(self):
        for path in modules(SOURCE):
            imported = imports(path)
            self.assertFalse(
                any(
                    item == "distribution" or item.startswith("distribution.") or item == "bootstrap"
                    for item in imported
                ),
                str(path.relative_to(ROOT)),
            )

    def test_internal_import_graph_has_no_cycles(self):
        paths = {name(path): path for path in modules(SOURCE)}
        graph: dict[str, set[str]] = defaultdict(set)
        for module, path in paths.items():
            for imported in imports(path):
                candidates = [item for item in paths if item == imported]
                graph[module].update(candidates)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(module: str, trail: tuple[str, ...]) -> None:
            if module in visiting:
                self.fail("circular import: " + " -> ".join((*trail, module)))
            if module in visited:
                return
            visiting.add(module)
            for dependency in sorted(graph[module]):
                visit(dependency, (*trail, module))
            visiting.remove(module)
            visited.add(module)

        for module in sorted(paths):
            visit(module, ())

    def test_side_effect_calls_stay_at_boundaries(self):
        for path in modules(SOURCE):
            relative = path.relative_to(SOURCE)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                call = node.func
                direct = call.id if isinstance(call, ast.Name) else None
                attribute = call.attr if isinstance(call, ast.Attribute) else None
                if relative.parts[0] == "tasks":
                    self.assertNotIn(direct, {"print", "input"}, str(relative))
                    self.assertFalse(
                        isinstance(call, ast.Attribute)
                        and isinstance(call.value, ast.Name)
                        and call.value.id == "sys"
                        and call.attr == "exit",
                        str(relative),
                    )
                if direct in {"print", "input"}:
                    self.assertEqual(relative.parts[0], "ui", str(relative))
                if attribute in MUTATIONS:
                    self.assertIn(
                        relative.as_posix(),
                        {"system/files.py", "system/activation.py", "system/work.py"},
                        str(relative),
                    )

    def test_subprocess_is_centralized(self):
        for path in modules(SOURCE):
            relative = path.relative_to(SOURCE)
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            uses_subprocess = any(
                isinstance(node, (ast.Import, ast.ImportFrom))
                and (
                    any(alias.name == "subprocess" for alias in node.names)
                    if isinstance(node, ast.Import)
                    else node.module == "subprocess"
                )
                for node in ast.walk(tree)
            )
            if uses_subprocess:
                self.assertEqual(relative.as_posix(), "system/run.py")

    def test_native_package_removal_and_migration_paths_do_not_exist(self):
        forbidden = (
            "pacman" + " -R",
            "pacman" + " -Rns",
            "--no" + "scriptlet",
            "suppress_remove_" + "scriptlets",
            "execute_" + "replacements",
            "replacement_" + "groups",
        )
        active_files = [
            *modules(SOURCE),
            *modules(TESTS),
            *modules(ROOT / "acceptance"),
            ROOT / "README.md",
            ROOT / "SECURITY.md",
            ROOT / "bootstrap" / "install",
            ROOT / "release" / "notes.md",
            ROOT / "site" / "index.html",
            *sorted((ROOT / "docs").glob("*.md")),
        ]
        for path in active_files:
            content = path.read_text(encoding="utf-8")
            for value in forbidden:
                self.assertNotIn(value, content, f"{value} remains in {path.relative_to(ROOT)}")

        for path in (*modules(SOURCE), *modules(ROOT / "acceptance")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                tokens = [
                    item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
                if "pacman" in tokens:
                    position = tokens.index("pacman")
                    self.assertFalse(
                        any(token.startswith("-" + "R") for token in tokens[position + 1 :]),
                        f"native removal command remains in {path.relative_to(ROOT)}",
                    )
                self.assertNotIn("--no" + "scriptlet", tokens)

        self.assertFalse((ROOT / "conflicts").exists())
        self.assertFalse((SOURCE / "packages" / "policy.py").exists())
        self.assertFalse((ROOT / "acceptance" / "migration.py").exists())
        vpn = (ROOT / "apps" / "pacman" / "vpn").read_text(encoding="utf-8")
        self.assertEqual(vpn, "mullvad-vpn\n")

    def test_mutation_lock_is_confined_to_the_application_boundary(self):
        users = []
        for path in modules(SOURCE):
            content = path.read_text(encoding="utf-8")
            if "MutationLock" in content:
                users.append(path.relative_to(SOURCE).as_posix())
        self.assertEqual(users, ["app.py", "system/lock.py"])

        tree = ast.parse((SOURCE / "app.py").read_text(encoding="utf-8"))
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            lock_call = any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "MutationLock"
                for item in node.items
            )
            installer_call = any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "run_installers"
                for child in ast.walk(node)
            )
            guarded = guarded or (lock_call and installer_call)
        self.assertTrue(guarded, "mutating task execution must remain inside MutationLock")


if __name__ == "__main__":
    unittest.main()
