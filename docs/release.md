# Release process

The public repository begins with one root commit and one annotated `1.0.0` tag. Patch
releases advance `main` linearly and add immutable annotated tags without rewriting the
root or earlier releases. Release workflows rebuild committed source and never write
generated files or commits back to `main`.

## Artifact construction

`release/build` creates two independent PyInstaller one-directory bundles in clean
temporary roots. The archive builder requires the resulting bytes to match, normalizes
member order, paths, ownership, modes, and timestamps, validates the exact member
manifest, and checks the committed SHA-256.

The build environment is the digest-pinned Arch image and pinned Python build
requirements. `release/components.json` records embedded component versions and the
archive carries the corresponding license texts. The four public assets are:

1. `doubt-1.0.5-x86_64.tar.gz`;
2. `SHA256SUMS`;
3. `COMPONENTS.json`;
4. `release-members.txt`.

`release/SHA256` is authoritative for the archive. The installer pins that digest and
the member-manifest digest. The installer is outside the archive, so its embedded
archive digest is non-circular.

## Publication order

1. CI, Security, and disposable Arch Acceptance pass for the release commit.
2. The annotated tag is verified, peeled to its commit, and confirmed reachable from
   linear `main` history.
3. The release assets are rebuilt, checked, and uploaded without overwrite.
4. Every public asset is downloaded independently and compared byte-for-byte.
5. Pages rebuilds the same release and copies the committed installer byte-for-byte.
6. The custom domain, TLS, installer bytes, checksums, and two isolated public runs are
   verified.

The current release title is `doubt 1.0.5`. It is published, not a draft or prerelease. A
publication failure stops Pages deployment; a digest mismatch is never overwritten or
retried as new content.

## Validation

The complete local gate is `./check`. `./acceptance/container/run` runs the candidate
installer through a local HTTP endpoint in a disposable Arch environment as a regular
user with sudo and a controlling terminal. It verifies first run, decline, failure,
cleanup, declaration preservation, and idempotent rerun without host mutation.

The final public acceptance uses exactly the installation command documented on the
landing page, twice, in a fresh disposable Arch environment.

Reconciliation is not a cross-system transaction. Completed pacman, AUR, Flatpak,
GitHub, SSH, Git, or Codex work remains completed after a later failure and is reported
accurately; doubt never claims rollback for it.
