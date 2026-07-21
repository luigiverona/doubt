# Security policy

## Reporting

Report suspected vulnerabilities privately through GitHub's security-advisory
interface for this repository. Do not open a public issue containing credentials,
private keys, authentication output, exploitable path details, or undisclosed attack
steps.

Include the affected version, threat model, reproduction conditions, and the smallest
sanitized evidence needed to evaluate the report. Never submit real `auth.json`
contents, tokens, private keys, browser cookies, or personal account identifiers.

## Supported scope

Security fixes target the latest published release and current `main`. `doubt` is an
Arch Linux workstation tool; it does not claim support for other distributions.

GitHub SSH trust is isolated in `~/.ssh/doubt_known_hosts`, pinned to GitHub's officially
published Ed25519, ECDSA, and RSA keys, and used with strict host checking. Doubt never
accepts arbitrary trust-on-first-use or rewrites unrelated user host entries. Modified,
malformed, linked, incorrectly owned, or incorrectly permissioned managed trust fails closed.

The security boundary includes command argument integrity, managed-path validation,
atomic file replacement, permission handling, package-conflict detection,
credential redaction, read-only planning and verification, and GitHub Actions supply
chain pins. It also includes bootstrap prerequisite checks, deterministic runtime
packaging, archive validation before extraction, embedded digest verification,
user-local launcher collision checks, and rollback-safe release activation. External
package repositories and authentication providers remain outside the project's control.

Automated checks are defense in depth. They do not replace review of changes that
touch subprocess execution, filesystem mutation, package installation, or credentials.

Native package conflict inspection completes before package mutation. Every conflict
blocks with manual-resolution guidance; `doubt` does not remove native packages,
perform source migrations, suppress package scriptlets, or clean orphans.

## Quick-install trust boundary

The canonical bootstrap is delivered by HTTPS from
`https://doubt.luigiverona.dev/install`. HTTPS authenticates that first-stage delivery.
The bootstrap embeds the exact current GitHub Release asset URL and SHA-256, rejects
a mismatch before archive inspection, validates the complete member allowlist and
metadata before extraction, and installs only into user-owned paths. It refuses root,
requires a controlling terminal, never invokes `sudo`, never installs prerequisites,
and never confirms application mutation.

This is not an independently signed trust chain. Control of the Pages deployment or
custom domain could replace both the bootstrap and its embedded expected hash. GitHub,
GitHub Pages, DNS, TLS certificate authorities, and the local operating system remain
trust dependencies. `SHA256SUMS` supports independent manual comparison but is not the
bootstrap's runtime trust anchor.

Installed releases live under `${XDG_DATA_HOME:-$HOME/.local/share}/doubt/releases`.
The `current` symlink switches only after archive and runtime validation, prior
releases are retained, and `~/.local/bin/doubt` is replaced only when absent or an
exact managed launcher. Same-version reuse requires matching local version and archive
metadata. These controls protect against accidental collision and partial activation;
they do not defend against a malicious process already controlling the same user.

## Threat and recovery model

The bare mutating invocation binds a per-user Linux abstract Unix socket before task
mutation. The socket has no filesystem entry or PID text to trust, remove, or race. A
concurrent mutating run fails promptly, and closing the socket—normally or during
process teardown—releases the kernel-owned name. Read-only plan and direct verify are
not blocked by the mutation lock.

Quiet package and build providers run with closed stdin in a dedicated process group.
On cancellation, Doubt sends that owned group `SIGTERM`, waits for a bounded two-second
grace period, then uses `SIGKILL` and a final bounded reap attempt if needed. This scope
includes descendants without targeting unrelated processes. Explicit GitHub and Codex
authentication remain outside that capture path and retain the controlling terminal.
Provider output and diagnostic tails remain bounded and redacted.

Managed atomic writes reject symlink targets, hard-linked files, type mismatches,
unowned targets, and existing symlinked parent components. New temporary files receive
their final mode before content is written; file data is flushed and synced before
replacement, and the containing directory is synced afterward. Failure before the
atomic replacement preserves the original, and failed temporary files are cleaned on
a best-effort basis. Mutation errors omit file content and underlying exception text.

These checks defend against accidental unsafe paths and common same-user substitution
attacks. They do not claim complete resistance to a malicious local process racing a
parent-directory replacement between inspection and a path-based operation. A user
with control of the account can also inspect that account's files and process state.
External repositories, package signatures, GitHub, OpenAI authentication, browsers,
and the operating system remain independent trust boundaries.

There is no global rollback. Package managers and remote services cannot participate
in one transaction. Valid completed work is preserved; recovery is `plan`, bare
`./install`, `verify`, then retry. Diagnostics identify incomplete components without
rendering tokens, private keys, authentication-file contents, OAuth responses,
cookies, or sensitive environment values.
