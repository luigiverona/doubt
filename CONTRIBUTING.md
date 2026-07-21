# Contributing

`doubt` targets current Arch Linux workstations. Changes must preserve the declared
task inventory, managed paths, terminal contract, and non-destructive reconciliation
model unless a focused defect fix explains the change.

## Development setup

Use a disposable virtual environment outside the repository:

```bash
python3 -m venv /tmp/doubt-dev
/tmp/doubt-dev/bin/pip install -r requirements/dev.txt
export PATH=/tmp/doubt-dev/bin:$PATH
```

No development dependency is required at runtime.

Repository-owned scripts use `#!/usr/bin/env bash` and are validated only with Bash.
GitHub Actions also declares Bash as its run-shell default; no POSIX-shell portability
contract is maintained.

## Quality gates

Run the complete local gate before submitting a change:

```bash
./check
```

Individual gates are available for diagnosis, for example `./check unit`,
`./check fault`, `./check distribution`, `./check typing`, `./check coverage`, and
`./check security`. The
complete gate runs unit and structural tests, Ruff lint and formatting policy, strict
mypy, statement and branch coverage, import and compile checks, shell and safe CLI
smoke tests, terminology and naming checks, secret and dangerous-pattern scans,
action-pin and dependency-advisory audits, version consistency, whitespace checks,
and generated-artifact detection.
The distribution gate independently builds the runtime archive twice, validates its
member allowlist and embedded digest, and builds the Pages artifact outside the
repository.

Tests must use temporary homes, fake runners, injected input, and deterministic
fixtures. They must not install packages, open browsers, authenticate accounts, or
write to the developer's managed workstation state.

Tracked `apps/` and `deps/` are release defaults and the writable state for a source
checkout. Installed-runtime tests must use an isolated `XDG_CONFIG_HOME`; they must
never edit the packaged release tree or a developer's real package declarations.

## Architecture

Follow [the architecture guide](docs/architecture.md). Source and test directories
and ordinary Python module stems use one lowercase word. Do not add catch-all modules.

Every release follows [the release procedure](docs/release.md). Published history and
tags are immutable.

## Git history

Use a focused branch and a meaningful pull request. Development may use several small,
reviewable commits; required CI, Security, and Acceptance checks must pass against the
current base. Main uses squash merge so each pull request becomes one curated linear
commit, and GitHub deletes merged remote branches automatically. Merge commits and
direct pushes to `main` are not part of the normal workflow.

Run `./acceptance/container/run` for the disposable Arch container layer. It uses an
existing Podman or Docker engine, or an existing unprivileged Bubblewrap setup with
delegated subordinate IDs. Do not install or reconfigure host isolation tooling just
to run this gate. Disposable-VM acceptance remains a separate manual release gate.
