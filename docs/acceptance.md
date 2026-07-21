# Acceptance

Acceptance complements unit and structural tests; it does not rerun them. The required
GitHub check uses the pinned Arch container in `.github/workflows/acceptance.yml`, and
`./acceptance/container/run` provides the same product boundary locally through an
existing Podman, Docker, or unprivileged Bubblewrap installation.

The focused gate proves:

- current app and dependency lists load on Arch;
- repository metadata uses pacman's read-only query boundary;
- an installed native conflict blocks before every mutation command;
- the official Mullvad packages install on a clean disposable target;
- old source variants are absent and unrelated native packages remain installed;
- package verification passes and a second run is idempotent;
- plan output is deterministic, task filtering works, and detailed output is safe;
- cancellation executes no tasks and removed interfaces remain rejected;
- archive, bootstrap, Pages, confirmation, lock, and managed-state behavior retain
  selected product-level smoke coverage;
- package-list help, deterministic listing, local checking, dry runs, isolated real
  add/remove, plan consumption, release-default immutability, and add/remove round trips
  retain the desired-state safety boundary;
- the read-only repository mount has no generated or modified state afterward.

The CI check separately owns unit/integration, architecture, typing, lint, formatting,
coverage, compile/import smoke, version, and deterministic distribution. Security owns
secret, dangerous-pattern, advisory, action-pin, and permission checks. This separation
keeps each required context meaningful and avoids running the same exact gate twice.

## Public bootstrap acceptance

For a published release, use a private temporary HOME/XDG tree and a pseudo-terminal to
run the actual HTTPS command. Verify the published archive digest, safe extraction,
user-local activation, argument forwarding, normal cancellation, same-version reuse,
launcher collision refusal, unrelated-release preservation, and absence of writes
outside the isolated tree. Run installed `pkg list`, `pkg check`, dry-run and real
add/remove, confirm the XDG desired state survives a same-version rerun, and confirm the
versioned release files remain unchanged. Never target the operator's real HOME.

## VM boundary

Container acceptance proves Arch userspace behavior, not boot, kernel, systemd, desktop,
or rollback behavior. `acceptance/vm/guide.md` retains the manual evidence format for a
release where those properties are affected or explicitly required. The helper is
read-only and never automates application confirmation.
