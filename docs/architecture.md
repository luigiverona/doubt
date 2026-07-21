# Architecture

`doubt` is a layered bootstrapper for one fresh Arch workstation. Its architecture
exists to make mutation reviewable and failures understandable, not to abstract every
package manager feature.

## Dependency direction

```text
core <- system <- packages <- sources <- tasks <- app <- cli
  ^                                           ^      ^
  +-------------------- ui -------------------+------+
```

Higher layers may import lower layers; lower layers never know the composition root or
CLI. `ui` consumes core results and is invoked only at application/process boundaries.
Structural tests enforce this direction, reject cycles, centralize subprocess use, and
keep filesystem mutation at its owned boundary.

Distribution code (`bootstrap/`, `distribution/`, `release/`, and `site/`) is outside
the runtime graph. The archive contains a self-contained executable, its project-owned
CPython runtime, immutable package defaults, component metadata, and required license
texts.

In a development checkout, the tracked `apps/` and `deps/` trees are the authoritative
writable desired state. Quick-installed releases are version-addressed and read-only,
so the bootstrap copies both complete trees once to
`${XDG_CONFIG_HOME:-$HOME/.config}/doubt/packages`. That user-owned tree is then the
only installed desired state and is preserved across reinstall and release activation.
There is no overlay or merge between packaged defaults and user declarations.

## Responsibility map

| Question | Single owner |
| --- | --- |
| Where are arguments parsed? | `doubt/cli.py` |
| Where are workflows composed? | `doubt/app.py` |
| Where is desired package state loaded? | `doubt/packages/lists.py` |
| Where are desired package declarations edited? | `doubt/packages/edit.py` |
| Where are package operands validated? | `doubt/packages/query.py` |
| Where is the conflict-safe package plan created? | `doubt/packages/resolve.py` |
| Where is confirmation enforced? | `doubt/ui/prompt.py`, called by `app.py` |
| Where are commands constructed? | source/task adapters |
| Where can subprocesses execute? | `doubt/system/run.py` |
| Where is mutation permitted? | confirmed `app.py` path, then source/task adapters |
| Where is post-install state checked? | `doubt/tasks/verify.py` |
| Where do infrastructure failures become product errors? | adapters and `app.py` |

Core dataclasses express shared invariants: execution mode, task order, install items,
results, and package relations. There is no service locator, mutable global context,
factory layer, or protocol without multiple implementations or a test boundary.

## Package boundary

Package managers remain authoritative. Doubt owns only the policy needed around them:

1. `lists.py` loads deterministic declarations and rejects duplicate or malformed
   entries.
2. `query.py` reads installed and remote metadata. Pacman's read-only `-Sp` transaction
   preview chooses repository dependency closure and virtual providers.
3. `resolve.py` combines explicit AUR metadata with that repository transaction and
   verifies every returned dependency/capability relation.
4. Installed and selected-package conflicts fail closed before source adapters run.
5. `sources/` installs only missing explicit packages/applications and verifies them.

This boundary intentionally does not implement general dependency solving. An AUR-only
dependency must be explicitly declared or repository-resolvable; otherwise planning
fails with no mutation. Pacman, `yay`, and Flatpak own downloads, dependency ordering,
runtime selection, signatures, and normal scriptlets.

No native-package conflict authorizes removal. Doubt does not migrate package sources,
clean orphans, or import old personal workstation state.

`packages/edit.py` is a separate local filesystem boundary. It validates the complete
tree and proposed state, changes one plain-text list through optimistic atomic
replacement or deletion, verifies the result, and restores the original bytes if
post-write validation fails. It has no command runner, source adapter, network client,
installation lock, or Git dependency.

## Mutation boundary

The bare command loads state and asks for confirmation before any task execution.
After confirmation, `app.py` takes a nonblocking abstract Unix-socket lock with no
filesystem residue, revalidates state, runs global native conflict preflight, and only
then calls mutating adapters. A declined run, plan, verify, or help request does not
acquire the mutation lock.

`CommandRunner` accepts argument sequences and never a shell command string. Package
providers receive closed stdin; explicit authentication alone receives the terminal. It marks
inspection versus mutation in detailed output, supports non-executing plans, separates
operands with `--` where supported, and converts missing executables or nonzero exits
to typed operational failures. Tests capture commands at this boundary and assert that
none follow cancellation or a failed preflight.

Quiet providers share one bounded capture path in normal and verbose modes. Each owns a
new process group so interruption can request graceful termination for the provider and
its descendants, escalate after two seconds, and reap without signaling unrelated
processes. Authentication commands deliberately keep the ordinary terminal process
model because their prompts must remain interactive.

Managed file changes use `system/files.py`, which validates ancestors, ownership,
types, links, permissions, confinement, atomic replacement, and durability. Task
modules do not print, prompt, or exit the process.

## Planning, installation, and verification

All installed paths use the same selected XDG desired-state root, loader, and task order:

- `plan` runs inspection and dry-run adapters, renders intended work, and never prompts;
- bare install preflights, confirms once, locks, revalidates, executes grouped work,
  and verifies automatically;
- `verify` runs independent inspection functions and never invokes repair paths.

Renderer state is derived from structured `InstallResult` values. Operational failures
carry a kind, component, safe message, and optional next step. Expected infrastructure
faults become stable nonzero output; programming errors remain visible to tests.

SSH host trust is a strict isolated file pinned to GitHub's official keys. Fish support
is one managed fragment that conditionally prepends only `$HOME/.local/bin`. Codex
profile migration preflights both pairs and every managed launcher before atomic rename.

## Validation boundaries

- CI: unit/integration behavior, structure, typing, lint, formatting, coverage,
  compile/import smoke, version, and deterministic distribution.
- Security: secrets, dangerous patterns, dependency advisories, action pins, and
  workflow permissions.
- Acceptance: focused product behavior in disposable Arch, including installation,
  cancellation, conflicts, verification, idempotency, and unrelated-package retention.
- Release/Pages: annotated-tag identity, deterministic assets, public byte comparison,
  checksum verification, asset-before-installer ordering, and rollback-safe deployment.

Unit tests use fake runners and temporary homes. Acceptance intentionally does not rerun
the unit/type/lint suite; it proves a different environment and trust boundary.

## Deferred progress sequencing

Codex setup currently reports both profile stage starts before the combined Codex task
returns profile results. The final states are accurate, but an authentication prompt can
appear after both headings, making the active profile less obvious than intended.

Passing renderer callbacks directly into migration and authentication functions would
couple task internals to presentation and create a second event contract beside
`InstallResult`. That would increase complexity in a patch whose purpose is hardening,
so the sequencing change is deferred. A future minor release should introduce typed,
task-owned progress events consumed by the application/rendering layer. The design must
keep tasks independent of terminal rendering, preserve deterministic order, retain
read-only plan and verify behavior, and test authentication, migration, failure,
interruption, and unchanged-profile sequences before replacing the current application-
level stage wrapper.
