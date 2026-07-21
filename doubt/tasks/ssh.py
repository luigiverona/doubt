from __future__ import annotations

from pathlib import Path

from ..core.failure import FailureKind, OperationalError
from ..core.result import InstallResult
from ..system import files
from ..system.run import CommandRunner
from .github import keys

SSH_KEYGEN = "ssh-keygen"
SSH_COMMAND = "ssh"
SSH_DIRECTORY_MODE = 0o700
PRIVATE_KEY_MODE = 0o600
PUBLIC_KEY_MODE = 0o644
SSH_CONFIG_MODE = 0o600
PRIVATE_KEY_NAME = "doubt"
PUBLIC_KEY_NAME = f"{PRIVATE_KEY_NAME}.pub"
SSH_CONFIG_NAME = "config"
MANAGED_CONFIG_NAME = "doubt_config"
KNOWN_HOSTS_NAME = "doubt_known_hosts"
KEY_COMMENT = "doubt-managed"
INCLUDE_DIRECTIVE = "Include ~/.ssh/doubt_config"
MANAGED_CONFIG = """Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/doubt
    IdentitiesOnly yes
    UserKnownHostsFile ~/.ssh/doubt_known_hosts
    StrictHostKeyChecking yes
"""
GITHUB_KNOWN_HOSTS = """github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
"""


def run(runner: CommandRunner, home: Path | None = None) -> list[InstallResult]:
    ensure_ssh_keygen(runner)

    home_directory = home if home is not None else runner.home_directory()
    ssh_directory = home_directory / ".ssh"
    private_key = ssh_directory / PRIVATE_KEY_NAME
    public_key = ssh_directory / PUBLIC_KEY_NAME
    ssh_config = ssh_directory / SSH_CONFIG_NAME
    managed_config = ssh_directory / MANAGED_CONFIG_NAME
    known_hosts = ssh_directory / KNOWN_HOSTS_NAME

    try:
        results: list[InstallResult] = []
        directory_action = ensure_ssh_directory(ssh_directory, runner.dry_run)
        if directory_action is not None:
            results.append(result(directory_action, "add"))

        key_result = ensure_managed_keypair(private_key, public_key, runner)
        if not runner.dry_run and not valid_keypair(
            ssh_directory,
            private_key,
            public_key,
            runner,
        ):
            key_result = result(f"validate {PRIVATE_KEY_NAME}", "fail")
        results.append(key_result)

        if key_result.status == "fail":
            return results

        results.append(reconcile_known_hosts(known_hosts, runner.dry_run))
        results.append(reconcile_managed_config(managed_config, runner.dry_run))
        results.append(reconcile_ssh_config(ssh_config, runner.dry_run))

        if not runner.dry_run:
            status = "ok" if valid_client_config(ssh_config, private_key, runner) else "fail"
            config_result = result("github.com SSH client configuration", status)
            results.append(config_result)
            if config_result.status == "fail":
                return results

        if runner.dry_run and key_result.status != "ok":
            results.extend(keys.plan_after_local_reconciliation(runner))
        else:
            results.extend(keys.synchronize(public_key, runner, home_directory))
        return results
    except OSError as error:
        raise OperationalError(
            FailureKind.PERMISSION_DENIAL,
            "ssh",
            "failed to manage doubt SSH identity",
        ) from error


def ensure_ssh_keygen(runner: CommandRunner) -> None:
    if not runner.command_exists(SSH_KEYGEN):
        raise OperationalError(
            FailureKind.UNAVAILABLE_EXECUTABLE,
            "ssh",
            "ssh-keygen is required for SSH setup; install openssh or run the deps task before the ssh task",
        )
    if not runner.command_exists(SSH_COMMAND):
        raise OperationalError(
            FailureKind.UNAVAILABLE_EXECUTABLE,
            "ssh",
            "ssh is required for SSH client configuration; install openssh or run the deps task before the ssh task",
        )


def ensure_ssh_directory(ssh_directory: Path, dry_run: bool) -> str | None:
    if ssh_directory.exists():
        if not ssh_directory.is_dir():
            raise OperationalError(
                FailureKind.DIRECTORY_TYPE_MISMATCH,
                "ssh",
                "~/.ssh exists but is not a directory",
            )
        if file_mode(ssh_directory) == SSH_DIRECTORY_MODE:
            return None
        if not dry_run:
            files.permissions(ssh_directory, SSH_DIRECTORY_MODE)
        return "set permissions on ~/.ssh"

    if not dry_run:
        files.directory(ssh_directory, SSH_DIRECTORY_MODE)
        files.permissions(ssh_directory, SSH_DIRECTORY_MODE)
    return "create ~/.ssh"


def ensure_managed_keypair(
    private_key: Path,
    public_key: Path,
    runner: CommandRunner,
) -> InstallResult:
    reject_unsafe_managed_path(private_key, "private key")
    reject_unsafe_managed_path(public_key, "public key")

    private_exists = private_key.exists()
    public_exists = public_key.exists()

    if not private_exists:
        action = f"replace orphaned {PUBLIC_KEY_NAME}" if public_exists else f"create {PRIVATE_KEY_NAME}"
        if not runner.dry_run:
            files.remove(public_key)
            generate_keypair(private_key, runner)
            set_key_permissions(private_key, public_key)
        return result(action, "add")

    private_mode_changed = ensure_mode(private_key, PRIVATE_KEY_MODE, runner.dry_run)
    derived_public = derive_public_key(private_key, runner)
    if not derived_public:
        action = f"replace invalid {PRIVATE_KEY_NAME}"
        if runner.dry_run and private_mode_changed:
            action = f"repair {PRIVATE_KEY_NAME}"
        if not runner.dry_run:
            replace_keypair(private_key, public_key, runner)
        return result(action, "add")

    if not public_exists:
        if not runner.dry_run:
            write_public_key(public_key, derived_public)
            set_key_permissions(private_key, public_key)
        return result(f"recover {PUBLIC_KEY_NAME}", "add")

    public_mode_changed = ensure_mode(public_key, PUBLIC_KEY_MODE, runner.dry_run)
    stored_public = read_public_key(public_key)
    if stored_public != derived_public:
        if not runner.dry_run:
            write_public_key(public_key, derived_public)
            set_key_permissions(private_key, public_key)
        return result(f"reconcile {PUBLIC_KEY_NAME}", "add")

    if private_mode_changed or public_mode_changed:
        return result(f"set permissions on {PRIVATE_KEY_NAME}", "add")
    return result(PRIVATE_KEY_NAME, "ok")


def reject_unsafe_managed_path(path: Path, label: str) -> None:
    if path.is_symlink():
        raise OperationalError(
            FailureKind.UNSAFE_SYMLINK,
            "ssh",
            f"managed SSH {label} path must not be a symbolic link",
        )
    if path.exists() and not path.is_file():
        raise OperationalError(
            FailureKind.FILE_TYPE_MISMATCH,
            "ssh",
            f"managed SSH {label} path must be a regular file",
        )


def replace_keypair(
    private_key: Path,
    public_key: Path,
    runner: CommandRunner,
) -> None:
    files.remove(private_key)
    files.remove(public_key)
    generate_keypair(private_key, runner)
    set_key_permissions(private_key, public_key)


def generate_keypair(private_key: Path, runner: CommandRunner) -> None:
    runner.run(
        [
            SSH_KEYGEN,
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            KEY_COMMENT,
            "-q",
            "-f",
            str(private_key),
        ]
    )


def derive_public_key(private_key: Path, runner: CommandRunner) -> str:
    output = runner.output([SSH_KEYGEN, "-y", "-P", "", "-f", str(private_key)])
    return public_identity(output)


def read_public_key(public_key: Path) -> str:
    try:
        return public_identity(public_key.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return ""


def public_identity(value: str) -> str:
    for line in value.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0] == "ssh-ed25519":
            return f"{fields[0]} {fields[1]}"
    return ""


def write_public_key(public_key: Path, identity: str) -> None:
    files.text(public_key, f"{identity} {KEY_COMMENT}\n", PUBLIC_KEY_MODE)


def set_key_permissions(private_key: Path, public_key: Path) -> None:
    files.permissions(private_key, PRIVATE_KEY_MODE)
    files.permissions(public_key, PUBLIC_KEY_MODE)


def ensure_mode(path: Path, desired_mode: int, dry_run: bool) -> bool:
    if file_mode(path) == desired_mode:
        return False
    if not dry_run:
        files.permissions(path, desired_mode)
    return True


def file_mode(path: Path) -> int:
    return files.mode(path)


def valid_keypair(
    ssh_directory: Path,
    private_key: Path,
    public_key: Path,
    runner: CommandRunner,
) -> bool:
    if not ssh_directory.is_dir() or file_mode(ssh_directory) != SSH_DIRECTORY_MODE:
        return False
    if not private_key.is_file() or private_key.is_symlink():
        return False
    if not public_key.is_file() or public_key.is_symlink():
        return False
    if file_mode(private_key) != PRIVATE_KEY_MODE:
        return False
    if file_mode(public_key) != PUBLIC_KEY_MODE:
        return False

    derived_public = derive_public_key(private_key, runner)
    return bool(derived_public) and read_public_key(public_key) == derived_public


def reconcile_managed_config(path: Path, dry_run: bool) -> InstallResult:
    reject_unsafe_managed_path(path, "client configuration")
    desired_content = MANAGED_CONFIG.encode("utf-8")
    exists = path.exists()
    content_changed = not exists or path.read_bytes() != desired_content
    mode_changed = exists and file_mode(path) != SSH_CONFIG_MODE

    if not dry_run:
        if content_changed:
            atomic_write(path, desired_content, SSH_CONFIG_MODE)
        elif mode_changed:
            files.permissions(path, SSH_CONFIG_MODE)

    if not exists:
        return result(f"create {MANAGED_CONFIG_NAME}", "add")
    if content_changed:
        return result(f"update {MANAGED_CONFIG_NAME}", "add")
    if mode_changed:
        return result(f"set permissions on {MANAGED_CONFIG_NAME}", "add")
    return result(MANAGED_CONFIG_NAME, "ok")


def reconcile_known_hosts(path: Path, dry_run: bool) -> InstallResult:
    reject_unsafe_managed_path(path, "GitHub host keys")
    desired = GITHUB_KNOWN_HOSTS.encode("ascii")
    exists = path.exists()
    if exists:
        ensure_owned_path(path, "GitHub host keys")
        current = path.read_bytes()
        if current != desired:
            raise OperationalError(
                FailureKind.UNSAFE_PATH,
                "ssh",
                "managed GitHub host-key file differs from official pinned material",
            )
    changed = not exists or (exists and file_mode(path) != SSH_CONFIG_MODE)
    if not dry_run:
        if not exists:
            atomic_write(path, desired, SSH_CONFIG_MODE)
        elif changed:
            files.permissions(path, SSH_CONFIG_MODE)
    return result("GitHub host trust", "add" if changed else "ok")


def ensure_owned_path(path: Path, label: str) -> None:
    try:
        from ..system import paths

        paths.owned(path, label)
    except OSError as error:
        raise OperationalError(FailureKind.PERMISSION_DENIAL, "ssh", f"could not inspect {label}") from error


def reconcile_ssh_config(path: Path, dry_run: bool) -> InstallResult:
    reject_unsafe_managed_path(path, "user configuration")
    exists = path.exists()
    current_content = path.read_bytes() if exists else b""
    desired_content = with_managed_include(current_content)
    content_changed = not exists or current_content != desired_content
    mode_changed = exists and file_mode(path) != SSH_CONFIG_MODE

    if not dry_run:
        if content_changed:
            atomic_write(path, desired_content, SSH_CONFIG_MODE)
        elif mode_changed:
            files.permissions(path, SSH_CONFIG_MODE)

    if not exists:
        return result(f"create {SSH_CONFIG_NAME} include", "add")
    if content_changed:
        return result(f"update {SSH_CONFIG_NAME} include", "add")
    if mode_changed:
        return result(f"set permissions on {SSH_CONFIG_NAME}", "add")
    return result(f"{SSH_CONFIG_NAME} include", "ok")


def with_managed_include(content: bytes) -> bytes:
    directive = INCLUDE_DIRECTIVE.encode("utf-8")
    lines = content.splitlines(keepends=True)
    matches = [line.rstrip(b"\r\n") == directive for line in lines]

    if sum(matches) == 1:
        return content
    if sum(matches) > 1:
        kept_directive = False
        reconciled: list[bytes] = []
        for line, matches_directive in zip(lines, matches, strict=True):
            if not matches_directive:
                reconciled.append(line)
            elif not kept_directive:
                reconciled.append(line)
                kept_directive = True
        return b"".join(reconciled)

    separator = b"" if not content or content.endswith(b"\n") else b"\n"
    return content + separator + directive + b"\n"


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    files.atomic(path, content, mode, prefix=f".{path.name}.doubt-")


def valid_client_config(
    ssh_config: Path,
    private_key: Path,
    runner: CommandRunner,
) -> bool:
    output = runner.output([SSH_COMMAND, "-G", "-F", str(ssh_config), "github.com"])
    if not output:
        return False

    user = ""
    hostname = ""
    identities_only = ""
    identity_files: list[str] = []
    for line in output.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        name, value = fields[0].lower(), fields[1]
        if name == "user":
            user = value
        elif name == "hostname":
            hostname = value
        elif name == "identityfile":
            identity_files.append(value)
        elif name == "identitiesonly":
            identities_only = value

    expected_identities = {
        "~/.ssh/doubt",
        str(private_key),
    }
    return (
        user == "git"
        and hostname == "github.com"
        and identities_only == "yes"
        and any(identity in expected_identities for identity in identity_files)
    )


def result(name: str, status: str) -> InstallResult:
    return InstallResult(
        name=name,
        source="ssh",
        category="ssh",
        status=status,
    )
