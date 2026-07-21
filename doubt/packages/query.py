"""Package metadata validation, parsing, and queries."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from ..core.failure import FailureKind, OperationalError
from ..system.run import CommandResult, CommandRunner
from .model import PackageMetadata, Relation

PACKAGE_NAME_RE = re.compile(r"^[a-z0-9@._+][a-z0-9@._+-]*$")
FLATPAK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*){2,}$")
RELATION_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9@._+][A-Za-z0-9@._+-]*)(?:(?P<operator>>=|<=|=|>|<)(?P<version>[^\s]+))?$"
)
FIELD_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9 -]+?)\s*:\s*(?P<value>.*)$")
NONE_VALUE = "None"
AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"
AUR_CONNECT_TIMEOUT = "5"
AUR_TOTAL_TIMEOUT = "15"
AUR_RETRIES = "2"
AUR_RETRY_MAX_TIME = "35"


class MetadataReader:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner
        self.cache: dict[tuple[str, str], PackageMetadata | None] = {}
        self.version_cache: dict[tuple[str, str], int] = {}

    def repository(self, name: str) -> PackageMetadata | None:
        return self._read_remote("pacman", name)

    def repository_transaction(self, targets: Sequence[str]) -> tuple[str, ...]:
        """Ask pacman for the dependency-complete, non-mutating transaction."""
        if not targets:
            return ()
        for target in targets:
            parse_relation(target)
        if not self.runner.command_exists("pacman"):
            raise failure(
                FailureKind.UNAVAILABLE_EXECUTABLE,
                "pacman is required for repository transaction metadata",
            )
        response = self.runner.capture(
            [
                "pacman",
                "-Sp",
                "--noconfirm",
                "--print-format",
                "%n",
                "--",
                *targets,
            ],
            env={"LC_ALL": "C"},
        )
        if response.returncode != 0:
            raise failure(
                FailureKind.PACKAGE_METADATA_FAILURE,
                "failed to preview the repository package transaction",
            )
        names: list[str] = []
        for line in response.stdout.splitlines():
            name = line.strip()
            if not name:
                continue
            validate_package_name(name)
            if name not in names:
                names.append(name)
        return tuple(names)

    def aur(self, name: str) -> PackageMetadata | None:
        validate_package_name(name)
        key = ("aur", name)
        if key in self.cache:
            return self.cache[key]
        if not self.runner.command_exists("curl"):
            raise failure(
                FailureKind.UNAVAILABLE_EXECUTABLE,
                "curl is required for AUR package conflict metadata",
            )
        response = self.runner.capture(
            [
                "curl",
                "--silent",
                "--show-error",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--tlsv1.2",
                "--connect-timeout",
                AUR_CONNECT_TIMEOUT,
                "--max-time",
                AUR_TOTAL_TIMEOUT,
                "--retry",
                AUR_RETRIES,
                "--retry-delay",
                "1",
                "--retry-max-time",
                AUR_RETRY_MAX_TIME,
                "--retry-connrefused",
                "--write-out",
                "\n%{http_code}",
                "--",
                f"{AUR_RPC}?arg[]={name}",
            ]
        )
        if response.returncode != 0:
            raise aur_transport_failure(name, response)
        body, separator, status_text = response.stdout.rpartition("\n")
        if not separator or not status_text.isdigit():
            # Test and alternate runner boundaries may provide only the captured body.
            # The real curl invocation always appends the status line.
            body, status = response.stdout, 200
        else:
            status = int(status_text)
        if status != 200:
            transient = status in {408, 429} or 500 <= status <= 599
            action = "; retry `doubt verify`" if transient else ""
            kind = FailureKind.REMOTE_METADATA_UNAVAILABLE if transient else FailureKind.PACKAGE_METADATA_FAILURE
            raise failure(kind, f"AUR metadata unavailable for {name}: HTTP {status}{action}")
        metadata = parse_aur_response(body, name)
        self.cache[key] = metadata
        return metadata

    def _read_remote(self, source: str, name: str) -> PackageMetadata | None:
        validate_package_name(name)
        key = (source, name)
        if key in self.cache:
            return self.cache[key]
        command = ["pacman", "-Si", "--", name]
        required = command[0]
        if not self.runner.command_exists(required):
            raise failure(
                FailureKind.UNAVAILABLE_EXECUTABLE,
                f"{required} is required for package conflict metadata",
            )
        response = self.runner.capture(command, env={"LC_ALL": "C"})
        if response.returncode != 0:
            if metadata_not_found(response):
                self.cache[key] = None
                return None
            raise failure(
                FailureKind.PACKAGE_METADATA_FAILURE,
                f"failed to read {source} metadata for {name}",
            )
        parsed = parse_metadata(response.stdout, source)
        if len(parsed) != 1 or parsed[0].name != name:
            raise failure(
                FailureKind.MALFORMED_PACKAGE_METADATA,
                f"invalid {source} metadata for {name}",
            )
        self.cache[key] = parsed[0]
        return parsed[0]

    def installed(self) -> tuple[PackageMetadata, ...]:
        if not self.runner.command_exists("pacman"):
            raise failure(
                FailureKind.UNAVAILABLE_EXECUTABLE,
                "pacman is required for installed package metadata",
            )
        response = self.runner.capture(["pacman", "-Qi"], env={"LC_ALL": "C"})
        if response.returncode != 0:
            raise failure(
                FailureKind.PACKAGE_METADATA_FAILURE,
                "failed to read installed package metadata",
            )
        foreign_response = self.runner.capture(["pacman", "-Qqm"], env={"LC_ALL": "C"})
        if foreign_response.returncode not in (0, 1):
            raise failure(
                FailureKind.PACKAGE_METADATA_FAILURE,
                "failed to identify foreign installed packages",
            )
        foreign = {line.strip() for line in foreign_response.stdout.splitlines() if line.strip()}
        packages = parse_metadata(response.stdout, "installed") if response.stdout.strip() else ()
        return tuple(
            PackageMetadata(
                package.name,
                "aur" if package.name in foreign else "pacman",
                package.version,
                package.dependencies,
                package.provides,
                package.conflicts,
                package.replaces,
            )
            for package in packages
        )

    def compare_versions(self, left: str, right: str) -> int:
        key = (left, right)
        if key in self.version_cache:
            return self.version_cache[key]
        if not self.runner.command_exists("vercmp"):
            raise failure(
                FailureKind.UNAVAILABLE_EXECUTABLE,
                "vercmp is required for versioned conflict metadata",
            )
        response = self.runner.capture(["vercmp", left, right], env={"LC_ALL": "C"})
        try:
            comparison = int(response.stdout.strip())
        except ValueError as error:
            raise failure(
                FailureKind.MALFORMED_COMMAND_OUTPUT,
                "failed to compare package versions",
            ) from error
        if response.returncode != 0 or comparison not in (-1, 0, 1):
            raise failure(
                FailureKind.PACKAGE_METADATA_FAILURE,
                "failed to compare package versions",
            )
        self.version_cache[key] = comparison
        return comparison


def validate_package_name(name: str) -> None:
    if not is_package_name(name):
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid native package name: {name!r}",
        )


def is_package_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and PACKAGE_NAME_RE.fullmatch(name) is not None
        and not name.startswith("-")
        and "/" not in name
        and ":" not in name
    )


def validate_flatpak_id(app_id: str) -> None:
    if not isinstance(app_id, str) or not FLATPAK_ID_RE.fullmatch(app_id):
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid Flatpak application ID: {app_id!r}",
        )


def parse_relation(value: str) -> Relation:
    match = RELATION_RE.fullmatch(value)
    if match is None:
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid package metadata relation: {value!r}",
        )
    return Relation(match.group("name"), match.group("operator"), match.group("version"), value)


def parse_metadata(output: str, source: str) -> tuple[PackageMetadata, ...]:
    packages: list[PackageMetadata] = []
    for block in parse_fields(output):
        name = one(block, "Name")
        version = one(block, "Version")
        validate_package_name(name)
        if not version:
            raise failure(
                FailureKind.MALFORMED_PACKAGE_METADATA,
                "package metadata is missing Version",
            )
        packages.append(
            PackageMetadata(
                name=name,
                source=source,
                version=version,
                dependencies=relations(block, ("Depends On", "Make Deps", "Check Deps")),
                provides=relations(block, ("Provides",)),
                conflicts=relations(block, ("Conflicts With",)),
                replaces=relations(block, ("Replaces",)),
            )
        )
    if not packages:
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            "package metadata response is empty or malformed",
        )
    return tuple(packages)


def parse_aur_response(output: str, expected_name: str) -> PackageMetadata | None:
    try:
        document = json.loads(output)
    except json.JSONDecodeError as error:
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid aur metadata for {expected_name}",
        ) from error
    if not isinstance(document, dict) or document.get("type") != "multiinfo":
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid aur metadata for {expected_name}",
        )
    results = document.get("results")
    if not isinstance(results, list) or len(results) > 1:
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid aur metadata for {expected_name}",
        )
    if not results:
        return None
    package = results[0]
    if not isinstance(package, dict):
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid aur metadata for {expected_name}",
        )
    name = package.get("Name")
    version = package.get("Version")
    if name != expected_name or not isinstance(version, str) or not version:
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"invalid aur metadata for {expected_name}",
        )
    validate_package_name(name)
    return PackageMetadata(
        name=name,
        source="aur",
        version=version,
        dependencies=aur_relations(package, ("Depends", "MakeDepends", "CheckDepends")),
        provides=aur_relations(package, ("Provides",)),
        conflicts=aur_relations(package, ("Conflicts",)),
        replaces=aur_relations(package, ("Replaces",)),
    )


def aur_relations(
    package: dict[str, object],
    fields: Sequence[str],
) -> tuple[Relation, ...]:
    values: list[str] = []
    for field in fields:
        entries = package.get(field, [])
        if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
            raise failure(
                FailureKind.MALFORMED_PACKAGE_METADATA,
                f"invalid aur metadata field: {field}",
            )
        values.extend(entries)
    return tuple(parse_relation(value) for value in values)


def parse_fields(output: str) -> list[dict[str, list[str]]]:
    blocks: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    current_name: str | None = None
    for line in [*output.splitlines(), ""]:
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
                current_name = None
            continue
        match = FIELD_RE.match(line)
        if match:
            current_name = match.group("name").strip()
            current[current_name] = split_values(match.group("value"))
        elif line[:1].isspace() and current_name is not None:
            current[current_name].extend(split_values(line.strip()))
        else:
            raise failure(
                FailureKind.MALFORMED_PACKAGE_METADATA,
                "malformed package metadata output",
            )
    return blocks


def split_values(value: str) -> list[str]:
    return [] if not value or value == NONE_VALUE else value.split()


def one(block: dict[str, list[str]], name: str) -> str:
    value = optional_one(block, name)
    if value is None:
        raise failure(
            FailureKind.MALFORMED_PACKAGE_METADATA,
            f"package metadata is missing {name}",
        )
    return value


def optional_one(block: dict[str, list[str]], name: str) -> str | None:
    entries = block.get(name, [])
    return " ".join(entries) if entries else None


def relations(block: dict[str, list[str]], names: Sequence[str]) -> tuple[Relation, ...]:
    return tuple(parse_relation(value) for name in names for value in block.get(name, ()))


def metadata_not_found(response: CommandResult) -> bool:
    message = f"{response.stdout}\n{response.stderr}"
    return "was not found" in message or "target not found" in message


def failure(kind: FailureKind, message: str) -> OperationalError:
    return OperationalError(kind, "packages", message)


def aur_transport_failure(name: str, response: CommandResult) -> OperationalError:
    labels = {
        5: "DNS resolution failed",
        6: "DNS resolution failed",
        7: "connection failed",
        28: "connection or total request timed out",
        35: "TLS negotiation failed",
        51: "TLS certificate validation failed",
        58: "TLS certificate validation failed",
        60: "TLS certificate validation failed",
        77: "TLS certificate validation failed",
        83: "TLS certificate validation failed",
        90: "TLS certificate validation failed",
        91: "TLS certificate validation failed",
    }
    reason = labels.get(response.returncode, f"curl failed with exit code {response.returncode}")
    return failure(
        FailureKind.REMOTE_METADATA_UNAVAILABLE,
        f"AUR metadata unavailable for {name}: {reason}; retry `doubt verify`",
    )
