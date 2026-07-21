import json
import unittest
from pathlib import Path
from unittest.mock import patch

from doubt.app import run_installers
from doubt.core.failure import FailureKind, OperationalError
from doubt.core.result import InstallResult
from doubt.packages import query
from doubt.packages import resolve as conflicts
from doubt.packages.lists import PackageList, load_lists
from doubt.system.run import CommandResult


def metadata_text(
    name,
    *,
    version="1.0-1",
    depends=(),
    make_deps=(),
    check_deps=(),
    provides=(),
    conflict_values=(),
    replaces=(),
):
    def field(label, entries):
        value = "  ".join(entries) if entries else "None"
        return f"{label:<30}: {value}\n"

    return "".join(
        (
            field("Name", (name,)),
            field("Version", (version,)),
            field("Provides", provides),
            field("Depends On", depends),
            field("Make Deps", make_deps),
            field("Check Deps", check_deps),
            field("Conflicts With", conflict_values),
            field("Replaces", replaces),
            "\n",
        )
    )


class FakeRunner:
    def __init__(self, repo=None, aur=None, installed=None, foreign=(), providers=None):
        self.dry_run = False
        self.repo = dict(repo or {})
        self.aur = dict(aur or {})
        self.installed = dict(installed or {})
        self.foreign = set(foreign)
        self.providers = dict(providers or {})
        self.calls = []
        self.commands = []
        self.available = {"curl", "pacman", "yay", "vercmp"}

    def command_exists(self, command):
        return command in self.available

    def capture(self, command, env=None):
        self.calls.append((list(command), env))
        if command == ["pacman", "-Qi"]:
            return CommandResult(0, "".join(self.installed.values()))
        if command == ["pacman", "-Qqm"]:
            output = "".join(f"{name}\n" for name in sorted(self.foreign))
            return CommandResult(0 if output else 1, output)
        if command[:3] == ["pacman", "-Si", "--"]:
            return self.remote_response(self.repo, command[3], "pacman")
        if command[:2] == ["pacman", "-Sp"]:
            targets = command[command.index("--") + 1 :]
            names: list[str] = []
            pending = list(targets)
            while pending:
                target = pending.pop(0)
                relation = conflicts.parse_relation(target)
                name = self.providers.get(target, relation.name)
                value = self.repo.get(name)
                if value is None or isinstance(value, CommandResult):
                    return CommandResult(1, stderr=f"error: target not found: {target}")
                if name in names:
                    continue
                names.append(name)
                package = conflicts.parse_metadata(value, "pacman")[0]
                pending.extend(dependency.original for dependency in package.dependencies)
            return CommandResult(0, "".join(f"{name}\n" for name in names))
        if command[0] == "curl":
            name = command[-1].split("?arg[]=", 1)[1]
            value = self.aur.get(name)
            if isinstance(value, CommandResult):
                return value
            results = [] if value is None else [self.aur_document(value)]
            return CommandResult(
                0,
                json.dumps({"version": 5, "type": "multiinfo", "resultcount": len(results), "results": results}),
            )
        if command[0] == "vercmp":
            left = self.version_key(command[1])
            right = self.version_key(command[2])
            return CommandResult(0, f"{(left > right) - (left < right)}\n")
        return CommandResult(2, stderr="unexpected command")

    @staticmethod
    def remote_response(database, name, source):
        value = database.get(name)
        if isinstance(value, CommandResult):
            return value
        if value is None:
            message = (
                f"error: package '{name}' was not found" if source == "pacman" else f"No AUR package found for {name}"
            )
            return CommandResult(1, stderr=message)
        return CommandResult(0, value)

    @staticmethod
    def aur_document(value):
        package = conflicts.parse_metadata(value, "aur")[0]
        return {
            "Name": package.name,
            "Version": package.version,
            "Depends": [item.original for item in package.dependencies],
            "MakeDepends": [],
            "CheckDepends": [],
            "Provides": [item.original for item in package.provides],
            "Conflicts": [item.original for item in package.conflicts],
            "Replaces": [item.original for item in package.replaces],
        }

    @staticmethod
    def version_key(value):
        return tuple(int(part) if part.isdigit() else part for part in value.replace("-", ".").split("."))

    def run(self, command, cwd=None):
        self.commands.append(list(command))

    def succeeds(self, command):
        return False


class PackageConflictTests(unittest.TestCase):
    @staticmethod
    def app_list(source, *names, category="dev"):
        return PackageList(source, category, tuple(names), Path(f"apps/{source}/{category}"))

    @staticmethod
    def dep_list(*names, category="bootstrap"):
        return PackageList("pacman", category, tuple(names), Path(f"deps/pacman/{category}"))

    def test_current_declared_inventory_is_complete_and_deterministic(self):
        inventory = conflicts.build_inventory(load_lists(Path("apps")), load_lists(Path("deps")), ("deps", "apps"))
        self.assertEqual(
            [package.name for package in inventory.native],
            [
                "git",
                "base-devel",
                "flatpak",
                "openai-codex",
                "nodejs",
                "ripgrep",
                "github-cli",
                "openssh",
                "torbrowser-launcher",
                "mullvad-vpn",
                "librewolf-bin",
                "visual-studio-code-bin",
            ],
        )
        self.assertEqual(
            [app.name for app in inventory.flatpak],
            [
                "net.mullvad.MullvadBrowser",
                "com.discordapp.Discord",
                "com.tutanota.Tutanota",
                "com.spotify.Client",
                "com.bitwarden.desktop",
            ],
        )

    def test_readme_package_tree_matches_tracked_categories(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        app_tree = readme.split("## App Lists", 1)[1].split("## Dependency Lists", 1)[0]
        dependency_tree = readme.split("## Dependency Lists", 1)[1].split("## Package sources", 1)[0]
        for path in sorted(Path("apps").glob("*/*")):
            self.assertIn(f"    {path.name}", app_tree)
        for path in sorted(Path("deps").glob("*/*")):
            self.assertIn(f"    {path.name}", dependency_tree)
        self.assertNotIn("stepssh", dependency_tree)

    def test_selection_limits_inventory_and_codex_closure(self):
        apps = [self.app_list("pacman", "app"), self.app_list("aur", "aur-app")]
        deps = [self.dep_list("dependency"), self.dep_list("codex", category="codex")]
        self.assertEqual(
            [item.name for item in conflicts.build_inventory(apps, deps, ("deps",)).native],
            ["dependency", "codex"],
        )
        self.assertEqual(
            [item.name for item in conflicts.build_inventory(apps, deps, ("apps",)).native],
            ["app", "aur-app"],
        )
        self.assertEqual(
            [item.name for item in conflicts.build_inventory(apps, deps, ("codex",)).native],
            ["codex"],
        )

    def test_duplicate_and_malformed_inventory_entries_are_rejected(self):
        cases = (
            ([self.dep_list("git"), self.dep_list("git", category="ssh")], [], "duplicate"),
            ([self.dep_list("-option")], [], "invalid native"),
            ([self.dep_list("repo/name")], [], "invalid native"),
            ([self.dep_list("name;rm")], [], "invalid native"),
            ([], [self.app_list("flatpak", "bad id")], "invalid Flatpak"),
            (
                [],
                [
                    self.app_list("flatpak", "com.example.App"),
                    self.app_list("flatpak", "com.example.App", category="chat"),
                ],
                "duplicate Flatpak",
            ),
        )
        for deps, apps, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(RuntimeError, message):
                    conflicts.build_inventory(apps, deps, ("deps", "apps"))

    def test_metadata_parser_handles_relations_and_none(self):
        parsed = conflicts.parse_metadata(
            metadata_text(
                "client",
                version="2.0-1",
                depends=("daemon>=2", "glibc"),
                conflict_values=("old-client<2",),
                replaces=("legacy=1",),
            ),
            "aur",
        )[0]
        self.assertEqual([item.name for item in parsed.dependencies], ["daemon", "glibc"])
        self.assertEqual(parsed.dependencies[0].operator, ">=")
        self.assertEqual(parsed.provides, ())
        self.assertEqual(parsed.conflicts[0].original, "old-client<2")

    def test_malformed_metadata_and_command_failures_fail_closed(self):
        with self.assertRaises(OperationalError) as malformed:
            conflicts.parse_metadata("garbage", "pacman")
        self.assertEqual(malformed.exception.kind, FailureKind.MALFORMED_PACKAGE_METADATA)
        runner = FakeRunner(repo={"broken": CommandResult(2, stderr="database failure")})
        reader = conflicts.MetadataReader(runner)
        with self.assertRaises(OperationalError) as unavailable:
            reader.repository("broken")
        self.assertEqual(unavailable.exception.kind, FailureKind.PACKAGE_METADATA_FAILURE)
        self.assertIsNone(reader.repository("missing"))

        aur_reader = conflicts.MetadataReader(FakeRunner())
        self.assertIsNone(aur_reader.aur("missing-aur"))
        with self.assertRaises(OperationalError) as aur_failure:
            conflicts.MetadataReader(FakeRunner(aur={"broken-aur": CommandResult(2, stderr="network")})).aur(
                "broken-aur"
            )
        self.assertEqual(aur_failure.exception.kind, FailureKind.REMOTE_METADATA_UNAVAILABLE)
        with self.assertRaises(OperationalError) as aur_malformed:
            conflicts.MetadataReader(FakeRunner(aur={"broken-aur": CommandResult(0, "not json")})).aur("broken-aur")
        self.assertEqual(aur_malformed.exception.kind, FailureKind.MALFORMED_PACKAGE_METADATA)

    def test_aur_http_failures_are_classified_without_caching(self):
        for status, transient in ((403, False), (404, False), (408, True), (429, True), (500, True), (502, True), (503, True)):
            with self.subTest(status=status):
                runner = FakeRunner(aur={"package": CommandResult(0, f"ignored\n{status}")})
                reader = conflicts.MetadataReader(runner)
                with self.assertRaisesRegex(OperationalError, f"HTTP {status}") as raised:
                    reader.aur("package")
                expected = FailureKind.REMOTE_METADATA_UNAVAILABLE if transient else FailureKind.PACKAGE_METADATA_FAILURE
                self.assertEqual(raised.exception.kind, expected)
                self.assertNotIn(("aur", "package"), reader.cache)

    def test_malformed_version_comparison_is_rejected(self):
        runner = FakeRunner()
        reader = conflicts.MetadataReader(runner)
        original = runner.capture

        def malformed(command, env=None):
            if command[0] == "vercmp":
                return CommandResult(0, "not-an-integer\n")
            return original(command, env)

        runner.capture = malformed
        with self.assertRaises(OperationalError) as raised:
            reader.compare_versions("1", "2")
        self.assertEqual(raised.exception.kind, FailureKind.MALFORMED_COMMAND_OUTPUT)

    def test_aur_rpc_validation_rejects_incomplete_or_ambiguous_metadata(self):
        invalid = (
            "[]",
            json.dumps({"type": "error", "results": []}),
            json.dumps({"type": "multiinfo", "results": "invalid"}),
            json.dumps({"type": "multiinfo", "results": [{}, {}]}),
            json.dumps({"type": "multiinfo", "results": ["invalid"]}),
            json.dumps(
                {
                    "type": "multiinfo",
                    "results": [{"Name": "other", "Version": "1"}],
                }
            ),
            json.dumps(
                {
                    "type": "multiinfo",
                    "results": [
                        {
                            "Name": "expected",
                            "Version": "1",
                            "Depends": "invalid",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "type": "multiinfo",
                    "results": [
                        {
                            "Name": "expected",
                            "Version": "1",
                            "Depends": [1],
                        }
                    ],
                }
            ),
        )
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(OperationalError) as raised:
                    query.parse_aur_response(document, "expected")
                self.assertEqual(raised.exception.kind, FailureKind.MALFORMED_PACKAGE_METADATA)

    def test_aur_rpc_requires_https_client_and_caches_valid_metadata(self):
        unavailable = FakeRunner()
        unavailable.available.remove("curl")
        with self.assertRaises(OperationalError) as raised:
            conflicts.MetadataReader(unavailable).aur("package")
        self.assertEqual(raised.exception.kind, FailureKind.UNAVAILABLE_EXECUTABLE)

        runner = FakeRunner(aur={"package": metadata_text("package")})
        reader = conflicts.MetadataReader(runner)
        first = reader.aur("package")
        second = reader.aur("package")
        self.assertEqual(first, second)
        self.assertEqual(len([call for call, _env in runner.calls if call[0] == "curl"]), 1)

    def test_metadata_tools_fail_closed_and_cache_verified_results(self):
        unavailable = FakeRunner()
        unavailable.available.remove("pacman")
        reader = conflicts.MetadataReader(unavailable)
        with self.assertRaises(OperationalError) as repository_error:
            reader.repository("package")
        self.assertEqual(repository_error.exception.kind, FailureKind.UNAVAILABLE_EXECUTABLE)
        with self.assertRaises(OperationalError) as installed_error:
            reader.installed()
        self.assertEqual(installed_error.exception.kind, FailureKind.UNAVAILABLE_EXECUTABLE)

        cached_runner = FakeRunner(repo={"package": metadata_text("package")})
        cached_reader = conflicts.MetadataReader(cached_runner)
        self.assertEqual(cached_reader.repository("package"), cached_reader.repository("package"))
        self.assertEqual(
            len([call for call, _env in cached_runner.calls if call[:3] == ["pacman", "-Si", "--"]]),
            1,
        )

        missing_vercmp = FakeRunner()
        missing_vercmp.available.remove("vercmp")
        with self.assertRaises(OperationalError) as comparison_error:
            conflicts.MetadataReader(missing_vercmp).compare_versions("1", "2")
        self.assertEqual(comparison_error.exception.kind, FailureKind.UNAVAILABLE_EXECUTABLE)

        comparison_runner = FakeRunner()
        comparison_reader = conflicts.MetadataReader(comparison_runner)
        self.assertEqual(comparison_reader.compare_versions("2", "1"), 1)
        self.assertEqual(comparison_reader.compare_versions("2", "1"), 1)
        self.assertEqual(
            len([call for call, _env in comparison_runner.calls if call[0] == "vercmp"]),
            1,
        )

    def test_installed_metadata_query_failures_are_rejected(self):
        runner = FakeRunner()

        def failed_installed(command, env=None):
            if command == ["pacman", "-Qi"]:
                return CommandResult(2, stderr="database failure")
            return CommandResult(2, stderr="unexpected command")

        runner.capture = failed_installed
        with self.assertRaises(OperationalError) as installed_error:
            conflicts.MetadataReader(runner).installed()
        self.assertEqual(installed_error.exception.kind, FailureKind.PACKAGE_METADATA_FAILURE)

        def failed_foreign(command, env=None):
            if command == ["pacman", "-Qi"]:
                return CommandResult(0, "")
            if command == ["pacman", "-Qqm"]:
                return CommandResult(2, stderr="database failure")
            return CommandResult(2, stderr="unexpected command")

        runner.capture = failed_foreign
        with self.assertRaises(OperationalError) as foreign_error:
            conflicts.MetadataReader(runner).installed()
        self.assertEqual(foreign_error.exception.kind, FailureKind.PACKAGE_METADATA_FAILURE)

    def test_metadata_commands_use_locale_and_operand_separator(self):
        runner = FakeRunner(repo={"git": metadata_text("git")}, aur={"aur-app": metadata_text("aur-app")})
        reader = conflicts.MetadataReader(runner)
        reader.repository("git")
        reader.aur("aur-app")
        self.assertIn((["pacman", "-Si", "--", "git"], {"LC_ALL": "C"}), runner.calls)
        curl = next(command for command, _env in runner.calls if command[0] == "curl")
        for required in ("--connect-timeout", "--max-time", "--retry", "--retry-max-time", "--proto-redir"):
            self.assertIn(required, curl)
        self.assertEqual(curl[-1], "https://aur.archlinux.org/rpc/v5/info?arg[]=aur-app")

    def test_repository_dependency_closure_is_fully_audited(self):
        runner = FakeRunner(
            repo={
                "mullvad-vpn": metadata_text("mullvad-vpn", depends=("mullvad-vpn-daemon",)),
                "mullvad-vpn-daemon": metadata_text("mullvad-vpn-daemon", depends=("dbus",)),
                "dbus": metadata_text("dbus"),
            }
        )
        inventory = conflicts.build_inventory([self.app_list("pacman", "mullvad-vpn", category="vpn")], [], ("apps",))
        desired = conflicts.resolve_desired(inventory.native, (), conflicts.MetadataReader(runner))
        self.assertEqual(
            [package.name for package in desired],
            ["mullvad-vpn", "mullvad-vpn-daemon", "dbus"],
        )

    def test_repository_transaction_resolves_virtual_dependency_with_pacman(self):
        runner = FakeRunner(
            repo={
                "app": metadata_text("app", depends=("libfixture.so=1-64",)),
                "fixture-provider": metadata_text("fixture-provider", provides=("libfixture.so=1-64",)),
            },
            providers={"libfixture.so=1-64": "fixture-provider"},
        )
        inventory = conflicts.build_inventory([self.app_list("pacman", "app")], [], ("apps",))
        desired = conflicts.resolve_desired(inventory.native, (), conflicts.MetadataReader(runner))
        self.assertEqual([package.name for package in desired], ["app", "fixture-provider"])
        self.assertIn(
            (
                [
                    "pacman",
                    "-Sp",
                    "--noconfirm",
                    "--print-format",
                    "%n",
                    "--",
                    "app",
                ],
                {"LC_ALL": "C"},
            ),
            runner.calls,
        )

    def test_repository_provider_mismatch_fails_closed(self):
        runner = FakeRunner(
            repo={
                "app": metadata_text("app", depends=("virtual-capability",)),
                "wrong-provider": metadata_text("wrong-provider"),
            },
            providers={"virtual-capability": "wrong-provider"},
        )
        results, safe = conflicts.preflight([self.app_list("pacman", "app")], [], ("apps",), runner)
        self.assertFalse(safe)
        self.assertIn("does not satisfy dependency virtual-capability", results[0].name)
        self.assertEqual(runner.commands, [])

    def test_repository_transaction_output_is_validated_and_deduplicated(self):
        runner = FakeRunner(
            repo={"fixture-provider": metadata_text("fixture-provider", provides=("virtual-capability",))},
            providers={"virtual-capability": "fixture-provider"},
        )
        reader = conflicts.MetadataReader(runner)
        relation = conflicts.parse_relation("virtual-capability")
        self.assertEqual(
            reader.repository_transaction((relation.original,)),
            ("fixture-provider",),
        )

        ambiguous = FakeRunner()
        original = ambiguous.capture

        def ambiguous_provider(command, env=None):
            if command[:2] == ["pacman", "-Sp"]:
                return CommandResult(0, "invalid/name\n")
            return original(command, env)

        ambiguous.capture = ambiguous_provider
        with self.assertRaises(OperationalError) as raised:
            conflicts.MetadataReader(ambiguous).repository_transaction((relation.original,))
        self.assertEqual(raised.exception.kind, FailureKind.MALFORMED_PACKAGE_METADATA)

    def test_dependency_provider_is_not_an_explicit_package_conflict(self):
        reader = conflicts.MetadataReader(FakeRunner())
        dependency = conflicts.parse_metadata(metadata_text("dependency"), "pacman")[0]
        provider = conflicts.parse_metadata(metadata_text("provider", provides=("dependency",)), "pacman")[0]
        self.assertIsNone(conflicts.detect_desired_conflict((dependency, provider), reader, origins={}))
        explicit = conflicts.detect_desired_conflict(
            (dependency, provider),
            reader,
            origins={"dependency": "apps/pacman/example"},
        )
        self.assertIsNotNone(explicit)
        self.assertIn("explicitly selected", explicit or "")

    def test_unresolved_transitive_dependency_fails_closed(self):
        runner = FakeRunner(repo={"app": metadata_text("app", depends=("unknown-virtual",))})
        results, safe = conflicts.preflight([self.app_list("pacman", "app")], [], ("apps",), runner)
        self.assertFalse(safe)
        self.assertIn("failed to preview", results[0].name)
        self.assertEqual(runner.commands, [])

    def test_undeclared_aur_dependency_fails_closed_without_recursive_resolution(self):
        runner = FakeRunner(
            aur={
                "a-bin": metadata_text("a-bin", depends=("b-bin",)),
                "b-bin": metadata_text("b-bin", depends=("a-bin",)),
            }
        )
        results, safe = conflicts.preflight([self.app_list("aur", "a-bin")], [], ("apps",), runner)
        self.assertFalse(safe)
        self.assertIn("failed to preview", results[0].name)
        self.assertEqual(
            len([call for call, _env in runner.calls if call[-1].endswith("=a-bin")]),
            1,
        )

    def test_direct_reverse_provider_replaces_and_versioned_conflicts_are_detected(self):
        reader = conflicts.MetadataReader(FakeRunner())
        desired = conflicts.parse_metadata(metadata_text("desired", conflict_values=("old>=2",)), "aur")[0]
        old = conflicts.parse_metadata(metadata_text("old", version="2"), "installed")[0]
        self.assertIn("conflicts", conflicts.conflict_relationship(desired, old, reader))

        reverse = conflicts.parse_metadata(
            metadata_text("alternative", provides=("desired",), conflict_values=("desired",)),
            "installed",
        )[0]
        self.assertIn("conflicts", conflicts.conflict_relationship(desired, reverse, reader))

        provider = conflicts.parse_metadata(metadata_text("provider", provides=("desired",)), "installed")[0]
        self.assertIn("provides", conflicts.conflict_relationship(desired, provider, reader))

        replacing = conflicts.parse_metadata(metadata_text("old", replaces=("desired",)), "installed")[0]
        self.assertIn("replaces", conflicts.conflict_relationship(desired, replacing, reader))

        nonmatching = conflicts.parse_metadata(metadata_text("old", version="1"), "installed")[0]
        self.assertIsNone(conflicts.conflict_relationship(desired, nonmatching, reader))

    def test_two_selected_packages_conflicting_is_configuration_failure(self):
        runner = FakeRunner(
            repo={
                "left": metadata_text("left", conflict_values=("right",)),
                "right": metadata_text("right"),
            }
        )
        results, safe = conflicts.preflight([self.app_list("pacman", "left", "right")], [], ("apps",), runner)
        self.assertFalse(safe)
        self.assertIn("selected packages conflict", results[0].name)
        self.assertIn("left", results[0].name)
        self.assertIn("right", results[0].name)
        self.assertEqual(runner.commands, [])

    @staticmethod
    def installed_conflict_runner(installed_name="code"):
        return FakeRunner(
            aur={
                "visual-studio-code-bin": metadata_text(
                    "visual-studio-code-bin", provides=("code",), conflict_values=("code",)
                )
            },
            installed={installed_name: metadata_text(installed_name)},
            foreign={installed_name},
        )

    def test_installed_conflict_stops_before_every_mutation_with_manual_guidance(self):
        runner = self.installed_conflict_runner()
        results, safe = conflicts.preflight([self.app_list("aur", "visual-studio-code-bin")], [], ("apps",), runner)
        self.assertFalse(safe)
        self.assertEqual(results[0].status, "fail")
        self.assertIn("cannot install visual-studio-code-bin", results[0].name)
        self.assertIn("installed code", results[0].name)
        self.assertIn("does not remove packages automatically", results[0].name)
        self.assertIn("resolve the conflict manually", results[0].name)
        self.assertEqual(runner.commands, [])

    def test_old_mullvad_variant_is_a_blocker_and_similar_name_is_not(self):
        desired = metadata_text("mullvad-vpn", depends=("mullvad-vpn-daemon",), conflict_values=("mullvad-vpn-bin",))
        daemon = metadata_text("mullvad-vpn-daemon")
        blocked_runner = FakeRunner(
            repo={"mullvad-vpn": desired, "mullvad-vpn-daemon": daemon},
            installed={"mullvad-vpn-bin": metadata_text("mullvad-vpn-bin")},
            foreign={"mullvad-vpn-bin"},
        )
        results, safe = conflicts.preflight(
            [self.app_list("pacman", "mullvad-vpn", category="vpn")],
            [],
            ("apps",),
            blocked_runner,
        )
        self.assertFalse(safe)
        self.assertIn("mullvad-vpn-bin", results[0].name)
        self.assertEqual(blocked_runner.commands, [])

        similar_runner = FakeRunner(
            repo={"mullvad-vpn": desired, "mullvad-vpn-daemon": daemon},
            installed={"mullvad-vpn-bin-extra": metadata_text("mullvad-vpn-bin-extra")},
            foreign={"mullvad-vpn-bin-extra"},
        )
        results, safe = conflicts.preflight(
            [self.app_list("pacman", "mullvad-vpn", category="vpn")],
            [],
            ("apps",),
            similar_runner,
        )
        self.assertTrue(safe)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(similar_runner.commands, [])

    def test_official_mullvad_packages_preflight_cleanly_and_repeat_idempotently(self):
        runner = FakeRunner(
            repo={
                "mullvad-vpn": metadata_text("mullvad-vpn", depends=("mullvad-vpn-daemon",)),
                "mullvad-vpn-daemon": metadata_text("mullvad-vpn-daemon"),
            }
        )
        apps = [self.app_list("pacman", "mullvad-vpn", category="vpn")]
        first, first_safe = conflicts.preflight(apps, [], ("apps",), runner)
        second, second_safe = conflicts.preflight(apps, [], ("apps",), runner)
        self.assertTrue(first_safe and second_safe)
        self.assertEqual(first[0].status, "ok")
        self.assertEqual(second[0].status, "ok")
        self.assertEqual(runner.commands, [])

    def test_target_metadata_failure_prevents_every_mutation(self):
        runner = FakeRunner(
            aur={"visual-studio-code-bin": CommandResult(2, stderr="network failure")},
            installed={"code": metadata_text("code")},
        )
        results, safe = conflicts.preflight([self.app_list("aur", "visual-studio-code-bin")], [], ("apps",), runner)
        self.assertFalse(safe)
        self.assertIn("AUR metadata unavailable", results[0].name)
        self.assertEqual(runner.commands, [])

    def test_flatpak_ids_are_not_sent_to_native_metadata(self):
        runner = FakeRunner()
        audited = conflicts.audit([self.app_list("flatpak", "com.example.App")], [], ("apps",), runner)
        self.assertEqual([item.name for item in audited.inventory.flatpak], ["com.example.App"])
        self.assertFalse(any("com.example.App" in command for command, _env in runner.calls))

    def test_verify_reports_conflict_without_repair(self):
        runner = self.installed_conflict_runner()
        result = conflicts.verify_conflicts([self.app_list("aur", "visual-studio-code-bin")], [], runner)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.source, "verify")
        self.assertIn("code", result.name)
        self.assertEqual(runner.commands, [])

    def test_preflight_and_verification_have_distinct_result_namespaces(self):
        runner = FakeRunner()
        preflight, safe = conflicts.preflight([], [], ("deps",), runner)
        verification = conflicts.verify_conflicts([], [], runner)
        self.assertTrue(safe)
        self.assertEqual(
            (preflight[0].category, preflight[0].source, preflight[0].name),
            ("packages", "packages", "conflict preflight"),
        )
        self.assertEqual(
            (verification.category, verification.source, verification.name),
            ("verify", "verify", "package conflicts"),
        )

    def test_global_preflight_failure_prevents_dependency_install(self):
        failure = InstallResult("installed conflict", "packages", "packages", "fail")
        runner = FakeRunner()
        with (
            patch("doubt.app.package_preflight.preflight", return_value=([failure], False)),
            patch("doubt.sources.pacman.install") as pacman_install,
        ):
            results = run_installers(
                [self.app_list("aur", "conflicting-app")],
                [self.dep_list("dependency")],
                runner,
                ("deps", "apps"),
            )
        self.assertEqual(results, [failure])
        pacman_install.assert_not_called()
        self.assertEqual(runner.commands, [])

    def test_run_installers_passes_exact_selected_closure_to_preflight(self):
        runner = FakeRunner()
        passed = InstallResult("conflict preflight", "packages", "packages", "ok")
        with (
            patch("doubt.app.package_preflight.preflight", return_value=([passed], True)) as preflight,
            patch("doubt.sources.pacman.install", return_value=[]),
        ):
            run_installers([], [self.dep_list("git")], runner, ("deps",))
        self.assertEqual(preflight.call_args.args[2], ("deps",))


if __name__ == "__main__":
    unittest.main()
