from __future__ import annotations

from ...core.failure import FailureKind, OperationalError
from ...core.result import InstallResult
from ...system.run import CommandRunner, InputPolicy

GH_COMMAND = "gh"
AUTH_STATUS_COMMAND = ["gh", "auth", "status"]
AUTH_LOGIN_COMMAND = [
    "gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "ssh",
    "--web", "--skip-ssh-key",
]
PROTOCOL_GET_COMMAND = [
    "gh",
    "config",
    "get",
    "git_protocol",
    "--host",
    "github.com",
]
PROTOCOL_SET_COMMAND = [
    "gh",
    "config",
    "set",
    "git_protocol",
    "ssh",
    "--host",
    "github.com",
]
PROTOCOL_RESULT_NAME = "GitHub Git operations use SSH"


def run(runner: CommandRunner) -> list[InstallResult]:
    ensure_gh(runner)
    authentication = reconcile_authentication(runner)
    if authentication.status == "fail":
        return [authentication]
    return [authentication, reconcile_git_protocol(runner)]


def reconcile_authentication(runner: CommandRunner) -> InstallResult:
    if is_authenticated(runner):
        return result("ok")

    if runner.dry_run:
        return result("add")

    runner.run(AUTH_LOGIN_COMMAND, input_policy=InputPolicy.TERMINAL)
    if is_authenticated(runner):
        return result("add")

    return result("fail")


def reconcile_git_protocol(runner: CommandRunner) -> InstallResult:
    current, error = current_git_protocol(runner)
    if error:
        return protocol_result("fail", error)
    if current == "ssh":
        return protocol_result("ok")
    if current not in ("", "https"):
        return protocol_result("fail", f"unexpected GitHub Git protocol: {current}")
    if runner.dry_run:
        return protocol_result("add")

    try:
        runner.run(PROTOCOL_SET_COMMAND)
    except OperationalError:
        return protocol_result("fail", "configure GitHub Git protocol to SSH")

    verified, verification_error = current_git_protocol(runner)
    if verification_error or verified != "ssh":
        return protocol_result("fail", "verify GitHub Git protocol is SSH")
    return protocol_result("add")


def current_git_protocol(runner: CommandRunner) -> tuple[str, str | None]:
    response = runner.capture(PROTOCOL_GET_COMMAND)
    current = response.stdout.strip()
    if runner.dry_run and response.failure is FailureKind.UNAVAILABLE_EXECUTABLE:
        return "", None
    if response.failure is not None or response.stderr.strip():
        return "", "inspect GitHub Git protocol"
    if not current:
        return "", None
    if response.returncode != 0:
        return "", "inspect GitHub Git protocol"
    return current, None


def ensure_gh(runner: CommandRunner) -> None:
    if not runner.command_exists(GH_COMMAND):
        raise OperationalError(
            FailureKind.UNAVAILABLE_EXECUTABLE,
            "github",
            "github-cli is required for GitHub setup; run the deps task before the github task",
        )


def is_authenticated(runner: CommandRunner) -> bool:
    return runner.succeeds(AUTH_STATUS_COMMAND)


def result(status: str) -> InstallResult:
    return InstallResult(
        name="github",
        source="auth",
        category="auth",
        status=status,
    )


def protocol_result(status: str, name: str = PROTOCOL_RESULT_NAME) -> InstallResult:
    return InstallResult(
        name=name,
        source="auth",
        category="auth",
        status=status,
    )
