from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...core.failure import FailureKind, OperationalError
from ...core.result import InstallResult
from ...system import files
from ...system.run import CommandResult, CommandRunner
from . import task

SCHEMA_VERSION = 1
CANONICAL_TITLE = "doubt"
STATE_DIRECTORY_MODE = 0o700
STATE_FILE_MODE = 0o600
STATE_RELATIVE_PATH = Path(".local/state/doubt/github-ssh-key.json")
AUTH_REFRESH_COMMAND = [
    "gh",
    "auth",
    "refresh",
    "-h",
    "github.com",
    "-s",
    "admin:public_key",
]


@dataclass(frozen=True)
class RemoteKey:
    key_id: int
    public_key: str
    title: str


@dataclass(frozen=True)
class OwnershipState:
    account_login: str
    remote_key: RemoteKey
    previous_remote_key: RemoteKey | None = None


def plan_after_local_reconciliation(runner: CommandRunner) -> list[InstallResult]:
    task.ensure_gh(runner)
    results: list[InstallResult] = []
    if not task.is_authenticated(runner):
        results.append(result("GitHub authentication required for SSH key sync", "add"))
    else:
        remote_keys, remote_error = fetch_remote_keys(runner)
        if remote_keys is None:
            if requires_public_key_scope(remote_error):
                results.append(result("GitHub SSH key API permission required", "add"))
            else:
                results.append(result("inspect GitHub SSH keys", "fail"))
    results.append(result("synchronize reconciled SSH key with GitHub", "add"))
    return results


def synchronize(
    public_key_path: Path,
    runner: CommandRunner,
    home: Path,
) -> list[InstallResult]:
    task.ensure_gh(runner)
    if not task.is_authenticated(runner):
        if runner.dry_run:
            return [result("GitHub authentication required for SSH key sync", "add")]
        if task.reconcile_authentication(runner).status == "fail":
            return [result("authenticate GitHub for SSH key sync", "fail")]

    public_key = read_public_identity(public_key_path)
    if not public_key:
        return [result("read managed public key for GitHub", "fail")]

    state_path = home / STATE_RELATIVE_PATH
    state = read_state(state_path)
    if not runner.dry_run:
        prepare_state_path(state_path)
    account = authenticated_login(runner)
    if not account:
        return [result("read authenticated GitHub account", "fail")]
    remote_keys, remote_error = fetch_remote_keys(runner)
    if remote_keys is None and requires_public_key_scope(remote_error):
        if runner.dry_run:
            return [result("GitHub SSH key API permission required", "add")]
        runner.run(AUTH_REFRESH_COMMAND)
        remote_keys, remote_error = fetch_remote_keys(runner)
    if remote_keys is None:
        return [result("list GitHub SSH keys", "fail")]

    if state is not None and state.account_login != account:
        return [result("GitHub SSH key ownership account mismatch", "fail")]

    current_remote = next(
        (key for key in remote_keys if key.public_key == public_key),
        None,
    )
    prior_remote, ownership_error = verified_prior_key(state, remote_keys)
    if ownership_error:
        return [result(ownership_error, "fail")]

    if current_remote is not None:
        if state is not None and prior_remote is None and state.remote_key.key_id != current_remote.key_id:
            return [result("GitHub SSH key ownership state is stale", "fail")]
        if state is None and current_remote.title != CANONICAL_TITLE:
            return [
                result(
                    "existing GitHub SSH key title preserved; ownership unproven",
                    "warn",
                )
            ]
        return reconcile_existing_remote(
            current_remote,
            prior_remote,
            state,
            state_path,
            account,
            runner,
        )

    if runner.dry_run:
        action = (
            "replace previously owned GitHub SSH key"
            if prior_remote is not None
            else "upload managed SSH key to GitHub"
        )
        return [result(action, "add")]

    new_remote, upload_error = upload_key(public_key, runner)
    if new_remote is None:
        return [result(upload_error, "fail")]
    verified_keys = list_remote_keys(runner)
    if verified_keys is None or not any(
        key.key_id == new_remote.key_id and key.public_key == public_key for key in verified_keys
    ):
        return [result("verify uploaded GitHub SSH key", "fail")]

    new_state = OwnershipState(account, new_remote, prior_remote)
    try:
        write_state(state_path, new_state)
    except (OSError, OperationalError):
        return [result("record GitHub SSH key ownership", "fail")]

    if prior_remote is not None and prior_remote.key_id != new_remote.key_id:
        cleanup_result = remove_owned_stale_key(
            prior_remote,
            new_remote,
            state_path,
            account,
            runner,
        )
        if cleanup_result is not None:
            return [cleanup_result]

    return verify_github_ssh(runner, "synchronize managed GitHub SSH key", "add")


def reconcile_existing_remote(
    current_remote: RemoteKey,
    prior_remote: RemoteKey | None,
    state: OwnershipState | None,
    state_path: Path,
    account: str,
    runner: CommandRunner,
) -> list[InstallResult]:
    stale = prior_remote
    if state is not None and state.previous_remote_key is not None:
        previous, error = verify_recorded_key(state.previous_remote_key, list_remote_keys(runner))
        if error:
            return [result(error, "fail")]
        stale = previous

    if runner.dry_run:
        if stale is not None and stale.key_id != current_remote.key_id:
            return [result("remove stale explicitly owned GitHub SSH key", "add")]
        if state is None or state.remote_key.key_id != current_remote.key_id:
            return [result("adopt existing GitHub SSH key ownership state", "add")]
        return [result("managed SSH key already synchronized with GitHub", "ok")]

    if stale is not None and stale.key_id != current_remote.key_id:
        cleanup_result = remove_owned_stale_key(
            stale,
            current_remote,
            state_path,
            account,
            runner,
        )
        if cleanup_result is not None:
            return [cleanup_result]

    desired_state = OwnershipState(account, current_remote)
    if state != desired_state:
        write_state(state_path, desired_state)
    return verify_github_ssh(runner, "managed SSH key synchronized with GitHub", "ok")


def remove_owned_stale_key(
    stale: RemoteKey,
    current: RemoteKey,
    state_path: Path,
    account: str,
    runner: CommandRunner,
) -> InstallResult | None:
    write_state(state_path, OwnershipState(account, current, stale))
    verified, error = verify_recorded_key(stale, list_remote_keys(runner))
    if error:
        return result(error, "fail")
    if verified is None:
        write_state(state_path, OwnershipState(account, current))
        return None
    if not delete_key(verified.key_id, runner):
        return result("new GitHub SSH key active; stale owned key remains", "warn")
    write_state(state_path, OwnershipState(account, current))
    return None


def verified_prior_key(
    state: OwnershipState | None,
    remote_keys: list[RemoteKey],
) -> tuple[RemoteKey | None, str | None]:
    if state is None:
        return None, None
    remote, error = verify_recorded_key(state.remote_key, remote_keys)
    if remote is not None or error is not None or state.previous_remote_key is None:
        return remote, error
    return verify_recorded_key(state.previous_remote_key, remote_keys)


def verify_recorded_key(
    recorded: RemoteKey,
    remote_keys: list[RemoteKey] | None,
) -> tuple[RemoteKey | None, str | None]:
    if remote_keys is None:
        return None, "list GitHub SSH keys for ownership verification"
    remote = next((key for key in remote_keys if key.key_id == recorded.key_id), None)
    if remote is None:
        return None, None
    if remote.public_key != recorded.public_key or remote.title != recorded.title:
        return None, "GitHub SSH key ownership metadata no longer matches"
    return remote, None


def read_public_identity(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    fields = value.strip().split()
    if len(fields) < 2 or not fields[0].startswith("ssh-"):
        return ""
    return f"{fields[0]} {fields[1]}"


def authenticated_login(runner: CommandRunner) -> str:
    response = runner.capture(["gh", "api", "user"])
    data = parse_json_response(response)
    return data.get("login", "") if isinstance(data, dict) else ""


def list_remote_keys(runner: CommandRunner) -> list[RemoteKey] | None:
    return fetch_remote_keys(runner)[0]


def fetch_remote_keys(
    runner: CommandRunner,
) -> tuple[list[RemoteKey] | None, str]:
    response = runner.capture(["gh", "api", "--paginate", "--slurp", "user/keys"])
    if response.returncode != 0:
        return None, response.stderr
    data = parse_json_response(response)
    if not isinstance(data, list):
        return None, "invalid GitHub SSH key response"
    pages = data if all(isinstance(item, list) for item in data) else [data]
    keys: list[RemoteKey] = []
    for page in pages:
        for item in page:
            if not isinstance(item, dict):
                return None, "invalid GitHub SSH key response"
            key_id, key, title = item.get("id"), item.get("key"), item.get("title")
            if not (isinstance(key_id, int) and isinstance(key, str) and isinstance(title, str)):
                return None, "invalid GitHub SSH key response"
            identity = normalize_public_key(key)
            if not identity:
                return None, "invalid GitHub SSH key response"
            keys.append(RemoteKey(key_id, identity, title))
    return keys, ""


def requires_public_key_scope(error: str) -> bool:
    return "admin:public_key" in error


def upload_key(
    public_key: str,
    runner: CommandRunner,
) -> tuple[RemoteKey | None, str]:
    return create_key(public_key, managed_title(public_key), runner)


def create_key(
    public_key: str,
    title: str,
    runner: CommandRunner,
) -> tuple[RemoteKey | None, str]:
    response = runner.capture(
        [
            "gh",
            "api",
            "--method",
            "POST",
            "user/keys",
            "-f",
            f"title={title}",
            "-f",
            f"key={public_key}",
        ]
    )
    if response.returncode != 0:
        if "key is already in use" in response.stderr.lower():
            return None, "managed SSH key is already registered to another account"
        return None, "GitHub rejected managed SSH key upload"
    data = parse_json_response(response)
    if not isinstance(data, dict):
        return None, "read GitHub SSH key upload response"
    key_id = data.get("id")
    returned_key = normalize_public_key(data.get("key", ""))
    returned_title = data.get("title")
    if not isinstance(key_id, int) or returned_key != public_key or returned_title != title:
        return None, "read GitHub SSH key upload response"
    return RemoteKey(key_id, public_key, title), ""


def remote_key_exists(remote_key: RemoteKey, runner: CommandRunner) -> bool:
    remote_keys = list_remote_keys(runner)
    return remote_keys is not None and remote_key in remote_keys


def delete_key(key_id: int, runner: CommandRunner) -> bool:
    response = runner.capture(["gh", "api", "--method", "DELETE", f"user/keys/{key_id}"])
    return response.returncode == 0


def verify_github_ssh(
    runner: CommandRunner,
    success_name: str,
    success_status: str,
) -> list[InstallResult]:
    response = runner.capture(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=yes",
            "git@github.com",
        ],
        env={"LC_ALL": "C"},
    )
    output = f"{response.stdout}\n{response.stderr}"
    success = (
        response.returncode == 1
        and "successfully authenticated" in output
        and "GitHub does not provide shell access" in output
    )
    name = success_name if success else "verify GitHub SSH authentication"
    status = success_status if success else "fail"
    return [result(name, status)]


def normalize_public_key(value: str) -> str:
    fields = value.strip().split()
    if len(fields) < 2 or not fields[0].startswith("ssh-"):
        return ""
    return f"{fields[0]} {fields[1]}"


def managed_title(_public_key: str) -> str:
    return CANONICAL_TITLE


def parse_json_response(response: CommandResult) -> object | None:
    if response.returncode != 0:
        return None
    try:
        parsed: object = json.loads(response.stdout)
        return parsed
    except json.JSONDecodeError:
        return None


def read_state(path: Path) -> OwnershipState | None:
    reject_unsafe_state_path(path.parent, directory=True)
    reject_unsafe_state_path(path, directory=False)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise invalid_state() from error
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise invalid_state()
    account = data.get("account_login")
    remote = key_from_state(data.get("remote_key"))
    previous_data = data.get("previous_remote_key")
    previous = key_from_state(previous_data) if previous_data is not None else None
    if (
        not isinstance(account, str)
        or not account
        or remote is None
        or (previous_data is not None and previous is None)
    ):
        raise invalid_state()
    return OwnershipState(account, remote, previous)


def key_from_state(value: object) -> RemoteKey | None:
    if not isinstance(value, dict):
        return None
    key_id = value.get("id")
    public_key = value.get("public_key")
    title = value.get("title")
    if not (isinstance(key_id, int) and isinstance(public_key, str) and isinstance(title, str)):
        return None
    identity = normalize_public_key(public_key)
    return RemoteKey(key_id, identity, title) if identity else None


def write_state(path: Path, state: OwnershipState) -> None:
    ensure_state_directory(path.parent)
    reject_unsafe_state_path(path, directory=False)
    data = {
        "schema_version": SCHEMA_VERSION,
        "account_login": state.account_login,
        "remote_key": key_to_state(state.remote_key),
        "previous_remote_key": (
            key_to_state(state.previous_remote_key) if state.previous_remote_key is not None else None
        ),
    }
    content = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    files.atomic(path, content, STATE_FILE_MODE, prefix=".github-ssh-key.")


def key_to_state(key: RemoteKey) -> dict[str, object]:
    return {"id": key.key_id, "public_key": key.public_key, "title": key.title}


def ensure_state_directory(path: Path) -> None:
    reject_unsafe_state_path(path, directory=True)
    if not path.exists():
        files.directory(path, STATE_DIRECTORY_MODE, parents=True)
    files.permissions(path, STATE_DIRECTORY_MODE)


def prepare_state_path(path: Path) -> None:
    ensure_state_directory(path.parent)
    reject_unsafe_state_path(path, directory=False)
    if path.exists():
        files.permissions(path, STATE_FILE_MODE)


def reject_unsafe_state_path(path: Path, directory: bool) -> None:
    if path.is_symlink():
        raise OperationalError(
            FailureKind.UNSAFE_SYMLINK,
            "github ssh",
            "unsafe GitHub SSH ownership state path",
        )
    if path.exists() and (path.is_dir() if directory else path.is_file()) is False:
        kind = FailureKind.DIRECTORY_TYPE_MISMATCH if directory else FailureKind.FILE_TYPE_MISMATCH
        raise OperationalError(
            kind,
            "github ssh",
            "unsafe GitHub SSH ownership state path",
        )


def invalid_state() -> OperationalError:
    return OperationalError(
        FailureKind.MALFORMED_JSON_STATE,
        "github ssh",
        "invalid GitHub SSH ownership state",
    )


def result(name: str, status: str) -> InstallResult:
    return InstallResult(name=name, source="github ssh", category="ssh", status=status)
