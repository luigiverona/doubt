from __future__ import annotations

import stat
from collections.abc import Sequence
from pathlib import Path

from ..core.failure import OperationalError
from ..core.result import InstallResult
from ..packages import resolve as conflicts
from ..packages.lists import PackageList
from ..system.run import CommandRunner
from . import codex, ssh
from . import git as git_task
from . import path as path_task
from .github import keys
from .github import task as github

VERIFY_AUTH_COMMAND = ["gh", "auth", "status", "--hostname", "github.com", "--active"]
PACKAGE_CHECK_COMMANDS = {
    "pacman": ["pacman", "-Qi"],
    "aur": ["yay", "-Q"],
    "flatpak": ["flatpak", "info"],
}


def run(
    app_lists: Sequence[PackageList],
    dependency_lists: Sequence[PackageList],
    runner: CommandRunner,
    home: Path | None = None,
    warn_only: bool = False,
) -> list[InstallResult]:
    home_directory = home if home is not None else runner.home_directory()
    results: list[InstallResult] = []
    results.append(conflicts.verify_conflicts(app_lists, dependency_lists, runner))
    results.extend(verify_packages(dependency_lists, runner, "dependency"))
    results.extend(verify_packages(app_lists, runner, "application"))

    github_results, account = verify_github(runner)
    results.extend(github_results)
    git_results = verify_git(home_directory, runner)
    results.extend(git_results)
    ssh_results = verify_local_ssh(home_directory, runner)
    results.extend(ssh_results)
    results.extend(codex.verify_state(runner, home=home_directory))
    results.append(path_task.verify(home_directory, getattr(runner, "environment", None)))

    local_ssh_ok = all(item.status == "ok" for item in ssh_results)
    github_ok = all(item.status == "ok" for item in github_results)
    if account and local_ssh_ok and github_ok:
        sync_result = verify_github_ssh_state(home_directory, account, runner)
        results.append(sync_result)
        if sync_result.status == "ok":
            results.append(verify_github_ssh_authentication(runner))
        else:
            results.append(fail("GitHub SSH authentication prerequisites"))
    else:
        results.append(fail("GitHub SSH synchronization prerequisites"))
        results.append(fail("GitHub SSH authentication prerequisites"))

    if warn_only:
        return [as_warning(item) for item in results]
    return results


def verify_packages(
    package_lists: Sequence[PackageList],
    runner: CommandRunner,
    kind: str,
) -> list[InstallResult]:
    results: list[InstallResult] = []
    for package_list in package_lists:
        check_command = PACKAGE_CHECK_COMMANDS[package_list.source]
        command = check_command[0]
        if not runner.command_exists(command):
            results.append(fail(f"{kind} check requires {command}"))
            continue
        for package in package_list.apps:
            response = runner.capture([*check_command, package])
            if response.returncode == 0:
                results.append(result(f"{kind} {package}", "ok"))
            elif response.returncode == 1:
                results.append(fail(f"missing {kind} {package}"))
            else:
                results.append(fail(f"{kind} check failed for {package}"))
    if not results:
        results.append(result(f"declared {kind}s", "ok"))
    return results


def verify_github(runner: CommandRunner) -> tuple[list[InstallResult], str]:
    if not runner.command_exists(github.GH_COMMAND):
        return [fail("GitHub authentication: github-cli is unavailable")], ""
    authenticated = runner.succeeds(VERIFY_AUTH_COMMAND)
    account = keys.authenticated_login(runner) if authenticated else ""
    auth_result = result(
        f"GitHub authentication {account}"
        if authenticated and account
        else "GitHub authentication for github.com is inactive",
        "ok" if authenticated and account else "fail",
    )
    protocol, error = github.current_git_protocol(runner)
    protocol_result = result(
        "GitHub protocol is SSH" if not error and protocol == "ssh" else "GitHub protocol must be SSH",
        "ok" if not error and protocol == "ssh" else "fail",
    )
    return [auth_result, protocol_result], account


def verify_git(home: Path, runner: CommandRunner) -> list[InstallResult]:
    if not runner.command_exists(git_task.GIT_COMMAND):
        return [fail("managed Git configuration: git is unavailable")]
    path = home / git_task.CONFIG_RELATIVE_PATH
    try:
        git_task.reject_unsafe_path(path.parent, directory=True)
        git_task.reject_unsafe_path(path, directory=False)
        if not path.parent.is_dir() or not path.is_file():
            return [fail("managed Git configuration is missing")]
        if git_task.file_mode(path.parent) != git_task.CONFIG_DIRECTORY_MODE:
            return [fail("managed Git configuration directory permissions")]
        if git_task.file_mode(path) != git_task.CONFIG_FILE_MODE:
            return [fail("managed Git configuration file permissions")]
        settings = git_task.read_config(path)
    except (OSError, OperationalError):
        return [fail("managed Git configuration is invalid or unsafe")]

    for key, attribute in git_task.MANAGED_KEYS:
        if git_task.global_value(runner, key) != getattr(settings, attribute):
            return [fail(f"managed Git configuration differs for {key}")]
    return [result("managed Git configuration", "ok")]


def verify_local_ssh(home: Path, runner: CommandRunner) -> list[InstallResult]:
    ssh_directory = home / ".ssh"
    private_key = ssh_directory / ssh.PRIVATE_KEY_NAME
    public_key = ssh_directory / ssh.PUBLIC_KEY_NAME
    managed_config = ssh_directory / ssh.MANAGED_CONFIG_NAME
    known_hosts = ssh_directory / ssh.KNOWN_HOSTS_NAME
    user_config = ssh_directory / ssh.SSH_CONFIG_NAME

    if not runner.command_exists(ssh.SSH_KEYGEN) or not runner.command_exists(ssh.SSH_COMMAND):
        return [fail("managed SSH verification requires openssh")]
    try:
        if ssh_directory.is_symlink() or not ssh_directory.is_dir() or mode(ssh_directory) != ssh.SSH_DIRECTORY_MODE:
            return [fail("managed SSH directory is missing, unsafe, or has wrong permissions")]
        for path in (private_key, public_key, managed_config, known_hosts, user_config):
            if path.is_symlink() or not path.is_file():
                return [fail(f"managed SSH file is missing or unsafe: {path.name}")]
        if mode(private_key) != ssh.PRIVATE_KEY_MODE:
            return [fail("managed SSH private key permissions")]
        if mode(public_key) != ssh.PUBLIC_KEY_MODE:
            return [fail("managed SSH public key permissions")]
        if not ssh.valid_keypair(ssh_directory, private_key, public_key, runner):
            return [fail("managed SSH keypair is invalid or mismatched")]
        identity_result = result("managed SSH identity", "ok")

        if mode(managed_config) != ssh.SSH_CONFIG_MODE or mode(user_config) != ssh.SSH_CONFIG_MODE:
            return [identity_result, fail("managed SSH configuration permissions")]
        if managed_config.read_bytes() != ssh.MANAGED_CONFIG.encode("utf-8"):
            return [identity_result, fail("managed SSH client configuration differs")]
        if mode(known_hosts) != ssh.SSH_CONFIG_MODE or known_hosts.read_bytes() != ssh.GITHUB_KNOWN_HOSTS.encode("ascii"):
            return [identity_result, fail("managed GitHub host trust differs")]
        include = ssh.INCLUDE_DIRECTIVE.encode("utf-8")
        include_count = sum(
            line.rstrip(b"\r\n") == include for line in user_config.read_bytes().splitlines(keepends=True)
        )
        if include_count != 1:
            return [identity_result, fail("managed SSH Include must appear exactly once")]
        if not ssh.valid_client_config(user_config, private_key, runner):
            return [identity_result, fail("effective github.com SSH configuration is invalid")]
        return [identity_result, result("managed SSH client configuration", "ok")]
    except (OSError, OperationalError):
        return [fail("managed SSH state is invalid or unsafe")]


def verify_github_ssh_state(
    home: Path,
    account: str,
    runner: CommandRunner,
) -> InstallResult:
    state_path = home / keys.STATE_RELATIVE_PATH
    public_key_path = home / ".ssh" / ssh.PUBLIC_KEY_NAME
    try:
        keys.reject_unsafe_state_path(state_path.parent, directory=True)
        keys.reject_unsafe_state_path(state_path, directory=False)
        if not state_path.parent.is_dir() or not state_path.is_file():
            return fail("GitHub SSH ownership state is missing")
        if mode(state_path.parent) != keys.STATE_DIRECTORY_MODE:
            return fail("GitHub SSH ownership state directory permissions")
        if mode(state_path) != keys.STATE_FILE_MODE:
            return fail("GitHub SSH ownership state file permissions")
        state = keys.read_state(state_path)
    except (OSError, OperationalError):
        return fail("GitHub SSH ownership state is invalid or unsafe")
    if state is None:
        return fail("GitHub SSH ownership state is missing")
    if state.account_login != account:
        return fail("GitHub SSH ownership account differs")
    if state.previous_remote_key is not None:
        return fail("GitHub SSH ownership state has pending stale-key cleanup")
    local_public = keys.read_public_identity(public_key_path)
    if not local_public or state.remote_key.public_key != local_public:
        return fail("GitHub SSH ownership public key differs")
    remote_keys = keys.list_remote_keys(runner)
    if remote_keys is None:
        return fail("GitHub SSH keys could not be inspected")
    remote = next(
        (key for key in remote_keys if key.key_id == state.remote_key.key_id),
        None,
    )
    if remote is None:
        return fail("managed GitHub SSH key is missing remotely")
    if remote.public_key != state.remote_key.public_key:
        return fail("managed GitHub SSH public key differs remotely")
    if remote.title != keys.CANONICAL_TITLE or state.remote_key.title != keys.CANONICAL_TITLE:
        return fail("managed GitHub SSH key title differs")
    if remote != state.remote_key:
        return fail("GitHub SSH ownership metadata differs remotely")
    return result("GitHub SSH synchronization", "ok")


def verify_github_ssh_authentication(runner: CommandRunner) -> InstallResult:
    checked = keys.verify_github_ssh(
        runner,
        "GitHub SSH authentication",
        "ok",
    )[0]
    return result(checked.name, checked.status)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def as_warning(item: InstallResult) -> InstallResult:
    if item.status != "fail":
        return item
    return result(item.name, "warn")


def fail(name: str) -> InstallResult:
    return result(name, "fail")


def result(name: str, status: str) -> InstallResult:
    return InstallResult(name=name, source="verify", category="verify", status=status)
