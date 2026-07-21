# Changelog

## 1.0.5 - 2026-07-21

- Bound interrupted package-provider cleanup with a two-second graceful termination
  period, process-group escalation, and child reaping.
- Redact and bound provider output consistently in normal and verbose modes while
  preserving interactive authentication and closed package stdin.
- Add read-only version reporting, explicit downgrade refusal, and migration-collision
  verification for supported upgrades from 1.0.2 and later.
- Correct the mutation-lock security model, document the upgrade matrix, and remove
  stale active Codex profile terminology.
- Eliminate test resource leaks and keep deterministic builds and disposable Arch
  acceptance release-critical.

## 1.0.4 - 2026-07-20

- Install Fish in the pinned release validation environment so the release-critical
  fresh-shell PATH test runs before assets are published.
- Carry forward the first-install reliability, secure GitHub host trust, bounded AUR
  metadata handling, numeric Codex migration, and launcher PATH integration prepared
  for the unpublished 1.0.3 tag.

## 1.0.3 - 2026-07-20

- Make normal first-install output concise, grouped, deterministic, and verification-led.
- Install Flatpak applications and runtimes after the single Doubt confirmation without
  a hidden package prompt; close package-provider stdin while retaining interactive auth.
- Pin GitHub's official SSH host keys in an isolated managed file with strict checking.
- Bound and classify AUR metadata retries without weakening conflict preflight.
- Migrate Codex profiles atomically to `.codex-01` and `.codex-02`, preserving all state.
- Add the narrow managed Fish fragment required for Doubt and Codex launchers.
- Show bootstrap download, verification, extraction, and startup phases.

## 1.0.2 - 2026-07-20

- Rename the managed Codex launchers to `codex-01` and `codex-02` while preserving
  their existing `.codex-personal` and `.codex-work` profile homes and authentication.
- Remove only byte-exact legacy launchers during reconciliation and refuse to delete
  modified or unsafe launcher paths.

## 1.0.1 - 2026-07-20

- Display the canonical product name and version in normal workflow output.
- Align primary workflow rows at column zero with stable blank-line separation.
- Suppress successful package-provider chatter in normal mode while retaining streamed
  provider output under `--verbose` and bounded diagnostics on failure.

## 1.0.0 - 2026-07-20

- Provide the complete fresh-Arch workflow through one verified curl command.
- Bundle the doubt-only Python runtime while installing declared workstation
  dependencies automatically after one confirmation.
- Keep planning and verification read-only, preserve user package declarations, and
  route all disposable downloads, builds, caches, and logs through one secure temporary
  root.
- Publish a small public CLI, concise grouped output, automatic verification, and a
  reproducible Arch x86_64 release.
