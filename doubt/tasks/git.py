from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallResult
from ..system import env as systemenv
from ..system import files, toml
from ..system.run import CommandRunner

GIT_COMMAND = "git"
CONFIG_RELATIVE_PATH = Path(".config/doubt/config.toml")
CONFIG_DIRECTORY_MODE = 0o700
CONFIG_FILE_MODE = 0o600
MANAGED_KEYS = (
    ("user.name", "name"),
    ("user.email", "email"),
    ("init.defaultBranch", "default_branch"),
)


@dataclass(frozen=True)
class GitSettings:
    name: str
    email: str
    default_branch: str


def run(
    runner: CommandRunner,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> list[InstallResult]:
    try:
        return _run(runner, home, environment, input_fn)
    except OSError as error:
        raise failure(
            FailureKind.PERMISSION_DENIAL,
            "failed to manage doubt Git configuration",
        ) from error


def _run(
    runner: CommandRunner,
    home: Path | None,
    environment: Mapping[str, str] | None,
    input_fn: Callable[[str], str] | None,
) -> list[InstallResult]:
    ensure_git(runner)
    home_directory = home if home is not None else runner.home_directory()
    config_path = home_directory / CONFIG_RELATIVE_PATH
    env = systemenv.current() if environment is None else environment

    reject_unsafe_path(config_path.parent, directory=True)
    reject_unsafe_path(config_path, directory=False)

    results: list[InstallResult] = []
    if config_path.exists():
        settings = read_config(config_path)
        permission_action = reconcile_permissions(config_path, runner.dry_run)
        if permission_action:
            results.append(task_result(permission_action, "add"))
    else:
        initial = initial_settings(runner, env, input_fn)
        if initial is None:
            return [
                task_result(
                    "Git identity requires DOUBT_GIT_NAME and DOUBT_GIT_EMAIL or interactive configuration",
                    "warn",
                )
            ]
        settings = initial
        results.append(task_result("create ~/.config/doubt/config.toml", "add"))
        if not runner.dry_run:
            write_config(config_path, settings)

    results.extend(reconcile_global_config(settings, runner))
    return results


def ensure_git(runner: CommandRunner) -> None:
    if not runner.command_exists(GIT_COMMAND):
        raise failure(
            FailureKind.UNAVAILABLE_EXECUTABLE,
            "git is required for managed Git configuration",
        )


def initial_settings(
    runner: CommandRunner,
    environment: Mapping[str, str],
    input_fn: Callable[[str], str] | None,
) -> GitSettings | None:
    explicit_name = "DOUBT_GIT_NAME" in environment
    explicit_email = "DOUBT_GIT_EMAIL" in environment
    name = environment.get("DOUBT_GIT_NAME")
    email = environment.get("DOUBT_GIT_EMAIL")
    branch = environment.get("DOUBT_GIT_DEFAULT_BRANCH", "main")

    if name is None:
        name = global_value(runner, "user.name") or None
    if email is None:
        email = global_value(runner, "user.email") or None

    missing_name = name is None and not explicit_name
    missing_email = email is None and not explicit_email
    if missing_name or missing_email:
        if runner.dry_run:
            return None
        if input_fn is None:
            raise failure(
                FailureKind.BLOCKED_PRECONDITION,
                "Git identity configuration requires interactive input",
            )
        if missing_name:
            name = prompt(input_fn, "Git name: ")
        if missing_email:
            email = prompt(input_fn, "Git email: ")

    return validate_settings(name, email, branch)


def prompt(input_fn: Callable[[str], str], message: str) -> str:
    try:
        return input_fn(message)
    except EOFError as error:
        raise failure(
            FailureKind.BLOCKED_PRECONDITION,
            "Git identity configuration requires interactive input",
        ) from error


def read_config(path: Path) -> GitSettings:
    try:
        data = toml.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, UnicodeDecodeError, OperationalError) as error:
        raise failure(
            FailureKind.MALFORMED_TOML,
            "invalid ~/.config/doubt/config.toml",
        ) from error
    git_data = data.get("git") if isinstance(data, dict) else None
    if not isinstance(git_data, dict):
        raise failure(
            FailureKind.MALFORMED_TOML,
            "config.toml requires a [git] section",
        )
    try:
        return validate_settings(
            git_data["name"],
            git_data["email"],
            git_data["default_branch"],
        )
    except KeyError as error:
        raise failure(
            FailureKind.MALFORMED_TOML,
            f"config.toml is missing git.{error.args[0]}",
        ) from error


def validate_settings(name: object, email: object, branch: object) -> GitSettings:
    valid_name = validate_text(name, "Git name")
    valid_email = validate_text(email, "Git email")
    valid_branch = validate_text(branch, "Git default branch")
    if (
        valid_email.count("@") != 1
        or not all(valid_email.split("@"))
        or any(character.isspace() for character in valid_email)
    ):
        raise failure(
            FailureKind.BLOCKED_PRECONDITION,
            "Git email must contain a plausible local and domain part",
        )
    if valid_branch.startswith("-"):
        raise failure(
            FailureKind.BLOCKED_PRECONDITION,
            "Git default branch must not begin with '-'",
        )
    return GitSettings(valid_name, valid_email, valid_branch)


def validate_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise failure(FailureKind.BLOCKED_PRECONDITION, f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned or "\n" in value or "\r" in value or "\0" in value:
        raise failure(FailureKind.BLOCKED_PRECONDITION, f"{label} is invalid")
    return cleaned


def reconcile_global_config(
    settings: GitSettings,
    runner: CommandRunner,
) -> list[InstallResult]:
    results: list[InstallResult] = []
    for key, attribute in MANAGED_KEYS:
        desired = getattr(settings, attribute)
        if global_value(runner, key) == desired:
            results.append(key_result(key, "ok"))
            continue
        if runner.dry_run:
            results.append(key_result(key, "add"))
            continue
        try:
            runner.run(["git", "config", "--global", key, desired])
        except OperationalError:
            results.append(key_result(key, "fail"))
            continue
        status = "add" if global_value(runner, key) == desired else "fail"
        results.append(key_result(key, status))
    return results


def global_value(runner: CommandRunner, key: str) -> str:
    response = runner.capture(["git", "config", "--global", "--get", key])
    return response.stdout.strip() if response.returncode == 0 else ""


def reconcile_permissions(path: Path, dry_run: bool) -> str | None:
    actions: list[str] = []
    if file_mode(path.parent) != CONFIG_DIRECTORY_MODE:
        actions.append("configuration directory permissions")
        if not dry_run:
            files.permissions(path.parent, CONFIG_DIRECTORY_MODE)
    if file_mode(path) != CONFIG_FILE_MODE:
        actions.append("configuration file permissions")
        if not dry_run:
            files.permissions(path, CONFIG_FILE_MODE)
    return "correct " + " and ".join(actions) if actions else None


def write_config(path: Path, settings: GitSettings) -> None:
    directory = path.parent
    files.directory(directory.parent, 0o777, parents=True, exist_ok=True)
    files.directory(directory, CONFIG_DIRECTORY_MODE, exist_ok=True)
    files.permissions(directory, CONFIG_DIRECTORY_MODE)
    content = (
        "[git]\n"
        f"name = {json.dumps(settings.name)}\n"
        f"email = {json.dumps(settings.email)}\n"
        f"default_branch = {json.dumps(settings.default_branch)}\n"
    ).encode()
    files.atomic(path, content, CONFIG_FILE_MODE, prefix=".config.")


def reject_unsafe_path(path: Path, directory: bool) -> None:
    if path.is_symlink():
        raise failure(FailureKind.UNSAFE_SYMLINK, "unsafe doubt Git configuration path")
    if path.exists() and (not path.is_dir() if directory else not path.is_file()):
        kind = FailureKind.DIRECTORY_TYPE_MISMATCH if directory else FailureKind.FILE_TYPE_MISMATCH
        raise failure(kind, "unsafe doubt Git configuration path")


def file_mode(path: Path) -> int:
    return files.mode(path)


def task_result(name: str, status: str) -> InstallResult:
    return InstallResult(name=name, source="git", category="git", status=status)


def key_result(key: str, status: str) -> InstallResult:
    return task_result(f"global {key}", status)


def failure(kind: FailureKind, message: str) -> OperationalError:
    return OperationalError(kind, "git", message)
