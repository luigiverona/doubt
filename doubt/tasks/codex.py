from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallResult
from ..system import files, paths, toml
from ..system.run import CommandRunner, InputPolicy

CODEX_BINARY = Path("/usr/bin/codex")
HOME_MODE = 0o700
CONFIG_MODE = 0o600
AUTH_MODE = 0o600
LAUNCHER_MODE = 0o755
CONFIG_NAME = "config.toml"
AUTH_NAME = "auth.json"
AUTH_STORE_KEY = "cli_auth_credentials_store"
AUTH_STORE_VALUE = "file"
REQUIRED_PACKAGES = ("openai-codex", "nodejs", "ripgrep")


@dataclass(frozen=True)
class Profile:
    label: str
    relative_home: Path
    launcher: str
    legacy_launcher: str | None = None
    legacy_home: Path | None = None


PROFILES = (
    Profile("01", Path(".codex-01"), "codex-01", "codex-personal", Path(".codex-personal")),
    Profile("02", Path(".codex-02"), "codex-02", "codex-work", Path(".codex-work")),
)


def run(
    runner: CommandRunner,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    binary: Path = CODEX_BINARY,
) -> list[InstallResult]:
    home_directory = (home if home is not None else runner.home_directory()).expanduser()
    validate_base_home(home_directory)
    profile_paths = tuple(home_directory / profile.relative_home for profile in PROFILES)
    validate_distinct_homes(profile_paths)
    preflight_launchers(home_directory / ".local" / "bin", binary)

    results: list[InstallResult] = migrate_profiles(home_directory, runner.dry_run)
    binary_ready = binary.is_file() and os.access(binary, os.X_OK)
    if binary == CODEX_BINARY and binary_ready and not official_binary(runner, binary):
        results.append(
            task_result(
                "/usr/bin/codex is not owned by Arch openai-codex; remove the unmanaged file before installation",
                "warn" if runner.dry_run else "fail",
            )
        )
        if not runner.dry_run:
            return results
        binary_ready = False
    if not binary_ready and not runner.dry_run:
        results.append(task_result("Codex CLI executable is unavailable", "fail"))
        return results
    for profile, profile_home in zip(PROFILES, profile_paths, strict=True):
        results.extend(reconcile_profile_home(profile, profile_home, runner.dry_run))

    launcher_directory = home_directory / ".local" / "bin"
    for profile in PROFILES:
        results.extend(
            reconcile_launcher(
                profile,
                launcher_directory,
                binary,
                runner.dry_run,
            )
        )

    for profile, profile_home in zip(PROFILES, profile_paths, strict=True):
        results.extend(authenticate(profile, profile_home, binary, runner, binary_ready))
    if not runner.dry_run and not any(result.status == "fail" for result in results):
        verification = verify_state(
            runner,
            home=home_directory,
            environment=environment,
            binary=binary,
        )
        results.extend(task_result(result.name, "fail") for result in verification if result.status == "fail")
    return results


def validate_base_home(home: Path) -> None:
    if not home.is_absolute() or home == Path("/") or not str(home).strip():
        raise OperationalError(
            FailureKind.UNSAFE_PATH,
            "codex",
            "invalid home directory for managed Codex state",
        )
    if home.is_symlink() or not home.is_dir():
        raise OperationalError(
            FailureKind.UNSAFE_PATH,
            "codex",
            "unsafe home directory for managed Codex state",
        )


def validate_distinct_homes(paths: tuple[Path, ...]) -> None:
    if len({path.resolve(strict=False) for path in paths}) != len(paths):
        raise OperationalError(
            FailureKind.UNSAFE_PATH,
            "codex",
            "Codex 01 and Codex 02 homes must be distinct",
        )
    existing = [path for path in paths if path.exists() and not path.is_symlink()]
    identities = {(path.stat().st_dev, path.stat().st_ino) for path in existing}
    if len(identities) != len(existing):
        raise OperationalError(
            FailureKind.UNSAFE_PATH,
            "codex",
            "Codex 01 and Codex 02 homes must be distinct",
        )


def profile_migrations(home: Path) -> list[tuple[Path, Path, Profile]]:
    moves: list[tuple[Path, Path, Profile]] = []
    for profile in PROFILES:
        if profile.legacy_home is None:
            continue
        source, destination = home / profile.legacy_home, home / profile.relative_home
        for candidate, label in ((source, "legacy"), (destination, "numeric")):
            reject_unsafe(candidate, directory=True, label=f"{label} Codex {profile.label} home")
            if candidate.exists():
                ensure_owned(candidate, f"{label} Codex {profile.label} home")
        if source.exists() and destination.exists():
            raise OperationalError(
                FailureKind.UNSAFE_PATH,
                "codex",
                f"Codex {profile.label} migration conflict: both legacy and numeric profile homes exist",
            )
        if source.exists():
            moves.append((source, destination, profile))
    return moves


def migrate_profiles(home: Path, dry_run: bool) -> list[InstallResult]:
    moves = profile_migrations(home)
    if dry_run:
        return [task_result(f"migrate Codex {profile.label} profile", "add") for _, _, profile in moves]
    completed: list[tuple[Path, Path, Profile]] = []
    try:
        for source, destination, profile in moves:
            os.rename(source, destination)
            completed.append((source, destination, profile))
    except OSError as error:
        for source, destination, _profile in reversed(completed):
            if destination.exists() and not source.exists():
                os.rename(destination, source)
        raise OperationalError(
            FailureKind.ATOMIC_WRITE_FAILURE, "codex", "Codex profile migration failed and was rolled back"
        ) from error
    return [task_result(f"migrate Codex {profile.label} profile", "add") for _, _, profile in moves]


def preflight_launchers(directory: Path, binary: Path) -> None:
    reject_unsafe(directory, directory=True, label="Codex launcher directory")
    if not directory.exists():
        return
    ensure_owned(directory, "Codex launcher directory")
    for profile in PROFILES:
        current = directory / profile.launcher
        reject_unsafe(current, directory=False, label=f"Codex {profile.label} launcher")
        if current.exists():
            content = read_launcher(current, profile.label)
            if content not in {
                launcher_content(profile, binary),
                old_launcher_content(profile, binary, profile.launcher),
            }:
                raise unmanaged_launcher(profile.launcher)
        if profile.legacy_launcher is not None:
            legacy = directory / profile.legacy_launcher
            reject_unsafe(legacy, directory=False, label=f"legacy Codex {profile.label} launcher")
            if legacy.exists() and read_launcher(legacy, f"legacy {profile.label}") != old_launcher_content(
                profile, binary, profile.legacy_launcher
            ):
                raise unmanaged_launcher(profile.legacy_launcher)


def reconcile_profile_home(
    profile: Profile,
    path: Path,
    dry_run: bool,
) -> list[InstallResult]:
    reject_unsafe(path, directory=True, label=f"{profile.label} Codex home")
    results: list[InstallResult] = []
    if not path.exists():
        results.append(task_result(f"create {profile.label} home", "add"))
        if dry_run:
            results.append(task_result(f"configure {profile.label} storage", "add"))
            return results
        files.directory(path, HOME_MODE)
    elif mode(path) != HOME_MODE:
        results.append(task_result(f"repair {profile.label} home permissions", "add"))
        if not dry_run:
            files.permissions(path, HOME_MODE)
    else:
        results.append(task_result(f"{profile.label} home", "ok"))
    ensure_owned(path, f"{profile.label} Codex home")

    config_path = path / CONFIG_NAME
    reject_unsafe(config_path, directory=False, label=f"{profile.label} Codex configuration")
    if not config_path.exists():
        results.append(task_result(f"configure {profile.label} storage", "add"))
        if not dry_run:
            files.text(
                config_path,
                f'{AUTH_STORE_KEY} = "{AUTH_STORE_VALUE}"\n',
                CONFIG_MODE,
            )
    else:
        ensure_owned(config_path, f"{profile.label} Codex configuration")
        content, configured = read_config(config_path)
        if not configured:
            results.append(task_result(f"configure {profile.label} storage", "add"))
            if not dry_run:
                files.text(config_path, update_config(content), CONFIG_MODE)
        elif mode(config_path) == CONFIG_MODE:
            results.append(task_result(f"{profile.label} storage", "ok"))
        if mode(config_path) != CONFIG_MODE:
            results.append(task_result(f"repair {profile.label} config permissions", "add"))
            if not dry_run:
                files.permissions(config_path, CONFIG_MODE)

    auth_path = path / AUTH_NAME
    reject_unsafe(auth_path, directory=False, label=f"{profile.label} Codex authentication")
    if auth_path.exists():
        ensure_owned(auth_path, f"{profile.label} Codex authentication")
        if mode(auth_path) != AUTH_MODE:
            results.append(
                task_result(
                    f"repair {profile.label} authentication permissions",
                    "add",
                )
            )
            if not dry_run:
                files.permissions(auth_path, AUTH_MODE)
    return results


def read_config(path: Path) -> tuple[str, bool]:
    try:
        content = path.read_text(encoding="utf-8")
        parsed = toml.parse(content, path.name)
    except (OSError, UnicodeDecodeError, OperationalError) as error:
        raise OperationalError(
            FailureKind.MALFORMED_TOML,
            "codex",
            f"invalid {path.name} in managed Codex home",
        ) from error
    return content, parsed.get(AUTH_STORE_KEY) == AUTH_STORE_VALUE


def update_config(content: str) -> str:
    lines = content.splitlines(keepends=True)
    in_top_level = True
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("["):
            in_top_level = False
        if not in_top_level or not stripped.startswith(AUTH_STORE_KEY):
            continue
        key, separator, _value = stripped.partition("=")
        if separator and key.strip() == AUTH_STORE_KEY:
            if '"""' in line or "'''" in line:
                raise OperationalError(
                    FailureKind.MALFORMED_TOML,
                    "codex",
                    "invalid multiline Codex credential-store setting",
                )
            comment = unquoted_comment(line)
            suffix = f" {comment.strip()}" if comment else ""
            newline = "\n" if line.endswith(("\n", "\r")) else ""
            lines[index] = f'{AUTH_STORE_KEY} = "{AUTH_STORE_VALUE}"{suffix}{newline}'
            return ensure_final_newline("".join(lines))
    addition = f'{AUTH_STORE_KEY} = "{AUTH_STORE_VALUE}"\n'
    return addition + ensure_final_newline(content) if content else addition


def unquoted_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = "" if quote == character else character if not quote else quote
            continue
        if character == "#" and not quote:
            return line[index:].rstrip("\r\n")
    return ""


def ensure_final_newline(content: str) -> str:
    return content.rstrip("\r\n") + "\n" if content else ""


def reconcile_launcher(
    profile: Profile,
    directory: Path,
    binary: Path,
    dry_run: bool,
) -> list[InstallResult]:
    ensure_launcher_directory(directory, dry_run)
    path = directory / profile.launcher
    reject_unsafe(path, directory=False, label=f"{profile.label} Codex launcher")
    desired = launcher_content(profile, binary)
    current = read_launcher(path, profile.label) if path.exists() else None
    recognized_old = old_launcher_content(profile, binary, profile.launcher)
    if current is not None and current not in {desired, recognized_old}:
        raise unmanaged_launcher(profile.launcher)

    legacy_path: Path | None = None
    legacy_exists = False
    legacy_expected: str | None = None
    if profile.legacy_launcher is not None:
        legacy_path = directory / profile.legacy_launcher
        reject_unsafe(
            legacy_path,
            directory=False,
            label=f"legacy {profile.label} Codex launcher",
        )
        if legacy_path.exists():
            legacy = read_launcher(legacy_path, f"legacy {profile.label}")
            legacy_expected = old_launcher_content(profile, binary, profile.legacy_launcher)
            if legacy != legacy_expected:
                raise unmanaged_launcher(profile.legacy_launcher)
            legacy_exists = True

    content_differ = current is not None and current != desired
    permissions_differ = current is not None and mode(path) != LAUNCHER_MODE
    if (current is None or content_differ) and not dry_run:
        files.text(path, desired, LAUNCHER_MODE)
    elif permissions_differ and not dry_run:
        files.permissions(path, LAUNCHER_MODE)
    if legacy_exists and legacy_path is not None and legacy_expected is not None and not dry_run:
        files.remove_if_unchanged(legacy_path, legacy_expected.encode("utf-8"))

    if legacy_exists:
        return [task_result(f"rename {profile.label} launcher", "add")]
    if current is None:
        return [task_result(f"create {profile.label} launcher", "add")]
    if content_differ:
        return [task_result(f"upgrade Codex {profile.label} launcher", "add")]
    if permissions_differ:
        return [task_result(f"repair {profile.label} launcher permissions", "add")]
    return [task_result(f"{profile.label} launcher", "ok")]


def read_launcher(path: Path, label: str) -> str:
    ensure_owned(path, f"{label} Codex launcher")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise OperationalError(
            FailureKind.FILE_TYPE_MISMATCH,
            "codex",
            f"invalid {label} Codex launcher",
        ) from error


def unmanaged_launcher(name: str) -> OperationalError:
    return OperationalError(
        FailureKind.UNSAFE_PATH,
        "codex",
        f"unsafe unmanaged launcher blocks {name}; move it and rerun setup",
    )


def ensure_launcher_directory(path: Path, dry_run: bool) -> None:
    local = path.parent
    reject_unsafe(local, directory=True, label="launcher parent")
    reject_unsafe(path, directory=True, label="launcher directory")
    if local.exists():
        ensure_owned(local, "launcher parent")
    if path.exists():
        ensure_owned(path, "launcher directory")
    if dry_run:
        return
    if not local.exists():
        files.directory(local, HOME_MODE)
    if not path.exists():
        files.directory(path, LAUNCHER_MODE)
    ensure_owned(local, "launcher parent")
    ensure_owned(path, "launcher directory")


def launcher_content(
    profile: Profile,
    binary: Path = CODEX_BINARY,
    *,
    launcher: str | None = None,
) -> str:
    launcher_name = profile.launcher if launcher is None else launcher
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ -z "${HOME:-}" ]; then\n'
        f'    echo "{launcher_name}: HOME is required" >&2\n'
        "    exit 1\n"
        "fi\n"
        f'CODEX_HOME="$HOME/{profile.relative_home}"\n'
        "export CODEX_HOME\n"
        f'exec "{binary}" "$@"\n'
    )


def old_launcher_content(profile: Profile, binary: Path, launcher: str) -> str:
    if profile.legacy_home is None:
        return ""
    old = Profile(profile.label, profile.legacy_home, launcher)
    return launcher_content(old, binary, launcher=launcher)


def authenticate(
    profile: Profile,
    home: Path,
    binary: Path,
    runner: CommandRunner,
    binary_ready: bool,
) -> list[InstallResult]:
    if not binary_ready:
        return [task_result(f"{profile.label} authentication requires interactive login", "warn")]
    environment = {"CODEX_HOME": str(home)}
    if login_status(binary, environment, runner):
        return [task_result(f"{profile.label} account", "ok")]
    if runner.dry_run:
        return [task_result(f"{profile.label} authentication requires interactive login", "warn")]

    notice = getattr(runner, "notice", None)
    if notice is not None:
        notice(
            f"{profile.label} account",
            f"sign in with the {profile.label} ChatGPT/OpenAI account",
        )
    try:
        runner.run([str(binary), "login"], env=environment, input_policy=InputPolicy.TERMINAL)
    except OperationalError:
        return [
            task_result(
                f"{profile.label} authentication failed; run {profile.launcher} login",
                "fail",
            )
        ]
    if not login_status(binary, environment, runner):
        return [
            task_result(
                f"{profile.label} authentication failed; run {profile.launcher} login",
                "fail",
            )
        ]
    auth_path = home / AUTH_NAME
    reject_unsafe(auth_path, directory=False, label=f"{profile.label} Codex authentication")
    if auth_path.exists():
        ensure_owned(auth_path, f"{profile.label} Codex authentication")
        if mode(auth_path) != AUTH_MODE:
            files.permissions(auth_path, AUTH_MODE)
    return [task_result(f"authenticate {profile.label} account", "add")]


def login_status(binary: Path, environment: Mapping[str, str], runner: CommandRunner) -> bool:
    return (
        runner.capture(
            [str(binary), "login", "status"],
            env=environment,
        ).returncode
        == 0
    )


def official_binary(runner: CommandRunner, binary: Path) -> bool:
    response = runner.capture(
        ["pacman", "-Qo", "--", str(binary)],
        env={"LC_ALL": "C"},
    )
    return response.returncode == 0 and " is owned by openai-codex " in response.stdout


def verify_state(
    runner: CommandRunner,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    binary: Path = CODEX_BINARY,
) -> list[InstallResult]:
    home_directory = (home if home is not None else runner.home_directory()).expanduser()
    failures: list[str] = []
    try:
        validate_base_home(home_directory)
        profile_paths = tuple(home_directory / profile.relative_home for profile in PROFILES)
        validate_distinct_homes(profile_paths)
    except (OSError, OperationalError):
        return [verification_result("Codex managed homes are invalid or unsafe", "fail")]

    try:
        pending_migrations = profile_migrations(home_directory)
    except (OSError, OperationalError) as error:
        failures.append(str(error))
    else:
        failures.extend(
            f"Codex {profile.label} profile requires migration; run doubt"
            for _source, _destination, profile in pending_migrations
        )

    for package in REQUIRED_PACKAGES:
        response = runner.capture(["pacman", "-Qi", "--", package], env={"LC_ALL": "C"})
        if response.returncode != 0:
            failures.append(f"Codex dependency {package} is missing")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        failures.append("Codex CLI executable is missing")
    elif binary == CODEX_BINARY and not official_binary(runner, binary):
        failures.append("Codex CLI executable is not owned by Arch openai-codex")
    launcher_directory = home_directory / ".local" / "bin"
    try:
        reject_unsafe(launcher_directory.parent, directory=True, label="launcher parent")
        reject_unsafe(launcher_directory, directory=True, label="launcher directory")
        if not launcher_directory.is_dir():
            failures.append("Codex launcher directory is missing")
        else:
            ensure_owned(launcher_directory.parent, "launcher parent")
            ensure_owned(launcher_directory, "launcher directory")
    except (OSError, OperationalError):
        failures.append("Codex launcher directory is invalid or unsafe")
    for profile, profile_home in zip(PROFILES, profile_paths, strict=True):
        try:
            reject_unsafe(profile_home, directory=True, label=f"{profile.label} Codex home")
            if not profile_home.is_dir() or mode(profile_home) != HOME_MODE:
                failures.append(f"Codex {profile.label} home is missing or has wrong permissions")
                continue
            ensure_owned(profile_home, f"{profile.label} Codex home")

            config_path = profile_home / CONFIG_NAME
            reject_unsafe(
                config_path,
                directory=False,
                label=f"{profile.label} Codex configuration",
            )
            if not config_path.is_file() or mode(config_path) != CONFIG_MODE:
                failures.append(f"Codex {profile.label} configuration is missing or has wrong permissions")
            else:
                ensure_owned(config_path, f"{profile.label} Codex configuration")
                _content, configured = read_config(config_path)
                if not configured:
                    failures.append(f"Codex {profile.label} credential storage differs")

            auth_path = profile_home / AUTH_NAME
            reject_unsafe(
                auth_path,
                directory=False,
                label=f"{profile.label} Codex authentication",
            )
            if auth_path.exists():
                ensure_owned(auth_path, f"{profile.label} Codex authentication")
                if mode(auth_path) != AUTH_MODE:
                    failures.append(f"Codex {profile.label} authentication permissions differ")

            launcher_path = launcher_directory / profile.launcher
            reject_unsafe(launcher_path, directory=False, label=f"{profile.label} Codex launcher")
            if (
                not launcher_path.is_file()
                or mode(launcher_path) != LAUNCHER_MODE
                or launcher_path.read_text(encoding="utf-8") != launcher_content(profile, binary)
            ):
                failures.append(f"Codex {profile.label} launcher is missing or differs")

            if profile.legacy_launcher is not None:
                legacy_launcher = launcher_directory / profile.legacy_launcher
                reject_unsafe(
                    legacy_launcher,
                    directory=False,
                    label=f"legacy {profile.label} Codex launcher",
                )
                if legacy_launcher.exists():
                    failures.append(f"Codex legacy {profile.label} launcher remains")

            if (
                binary.is_file()
                and os.access(binary, os.X_OK)
                and not login_status(
                    binary,
                    {"CODEX_HOME": str(profile_home)},
                    runner,
                )
            ):
                failures.append(
                    f"Codex {profile.label} account authentication is missing; run {profile.launcher} login"
                )
        except (OSError, OperationalError, UnicodeDecodeError):
            failures.append(f"Codex {profile.label} state is invalid or unsafe")

    if failures:
        return [verification_result(item, "fail") for item in dict.fromkeys(failures)]
    return [verification_result("Codex dual-account setup", "ok")]


def reject_unsafe(path: Path, directory: bool, label: str) -> None:
    paths.reject(path, directory=directory, label=label, links=True)


def ensure_owned(path: Path, label: str) -> None:
    paths.owned(path, label)


def mode(path: Path) -> int:
    return files.mode(path)


def task_result(name: str, status: str) -> InstallResult:
    return InstallResult(name=name, source="codex", category="codex", status=status)


def verification_result(name: str, status: str) -> InstallResult:
    return InstallResult(name=name, source="verify", category="verify", status=status)
