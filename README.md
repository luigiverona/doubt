# doubt

`doubt` is a focused fresh-Arch workstation bootstrapper.

```bash
curl -fsSL https://doubt.luigiverona.dev/install | bash
```

That is the complete normal workflow. The installer downloads and verifies the
self-contained 1.0.5 release, runs it from a private temporary directory, shows the
complete plan, asks once, reconciles the workstation, verifies the result, and removes
all disposable data. Python and project build tools are bundled and are not installed
on the workstation for doubt. The command itself necessarily uses Bash and curl as its
transport.

The supported target is Arch Linux or an Arch-based x86_64 system with Bash, curl,
pacman, sudo access where package installation requires it, ordinary Arch base
utilities, network access, and an interactive terminal.

## What a run does

A normal run has one concise preflight, one plan, one explicit confirmation, grouped
progress, automatic verification, and one final summary. Nothing persistent is written
before confirmation. Sudo and explicit authentication remain interactive when user
action is required. Routine provider output is suppressed in normal mode; `--verbose`
shows bounded, redacted command and provider detail.

Running the same installation command again is safe. Existing valid user package
declarations are preserved, the verified release is activated atomically, only missing
or incorrect managed state is reconciled, and the complete result is verified again.

## App Lists

```text
apps/
  pacman/
    browser
    vpn
  aur/
    browser
    dev
  flatpak/
    browser
    chat
    mail
    music
    pass
```

## Dependency Lists

```text
deps/
  pacman/
    bootstrap
    codex
    github
    ssh
```

## Commands

```text
doubt
doubt plan
doubt verify
doubt pkg list
doubt pkg add SOURCE CATEGORY PACKAGE
doubt pkg remove SOURCE PACKAGE
doubt pkg check
doubt --version
doubt --help
```

`doubt` plans, confirms once, applies every required change, and verifies automatically.
`doubt plan` and `doubt verify` are read-only. `doubt pkg` changes declarations only; it
never installs or removes a package. `doubt --verbose` exposes narrowly scoped command
diagnostics for troubleshooting. `doubt --version` reports the active managed runtime
without inspecting or changing workstation state.

Examples of declaration-only changes:

```bash
doubt pkg list
doubt pkg add pacman browser firefox
doubt pkg remove pacman firefox
doubt pkg check
```

Sources are `pacman`, `aur`, and `flatpak`. Categories are lowercase filesystem-safe
names. Package names and external operands are validated before use. Duplicate adds and
missing removals are successful no-ops.

## Managed state

The immutable release defaults are materialized deterministically on the first
confirmed run. Installed declarations live separately at:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/doubt/packages
${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/doubt-path.fish
```

Reinstallation never overwrites an existing valid declaration tree. The current
defaults declare these applications and workstation dependencies:

- pacman: `torbrowser-launcher`, `mullvad-vpn`, `git`, `base-devel`, `flatpak`,
  `openai-codex`, `nodejs`, `ripgrep`, `github-cli`, and `openssh`;
- AUR: `librewolf-bin` and `visual-studio-code-bin`;
- Flatpak: Mullvad Browser, Discord, Tuta, Spotify, and Bitwarden.

Missing workstation dependencies are included in preflight and installed after the
single confirmation in deterministic order. AUR work, makepkg output, downloads,
caches, and command logs are routed into one private temporary root.

Project-owned persistent paths are limited to:

```text
~/.local/bin/doubt
${XDG_DATA_HOME:-$HOME/.local/share}/doubt
${XDG_CONFIG_HOME:-$HOME/.config}/doubt/packages
```

There are no project caches, state databases, or persistent logs. The Fish fragment is
the narrow exception: it adds only `$HOME/.local/bin` when absent and is reversible by
removing that one managed file. It does not edit `config.fish` or other shells.
A declined run leaves none of these paths behind when they did not already exist.

The managed Codex launchers use numeric profile homes:

```text
codex-01  CODEX_HOME="$HOME/.codex-01"
codex-02  CODEX_HOME="$HOME/.codex-02"
```

Upgrading atomically renames exact previous release profile homes only after both
source/destination pairs and launchers pass safety preflight. It preserves credentials,
sessions, caches, logs, unknown files, ownership, and permissions; it never merges.
Direct upgrades from 1.0.2 and later are supported. A legacy and numeric profile home
for the same account is an explicit manual conflict, and verification reports it without
changing either directory. Downgrades are blocked when a newer managed runtime is active.

## Scope and safety

doubt manages package desired state, pacman and AUR packages, Flatpak applications,
GitHub authentication and setup, managed SSH setup, Git setup, and the Codex profile.
It does not install Arch, manage storage or boot, upgrade the system, remove packages,
clean orphans, migrate package sources, manage generic services, roll back external
systems, or manage dotfiles, shells, Hyprland, Caelestia, themes, or desktop ricing.

The important boundaries are:

- preflight, planning, and verification use inspection paths only;
- package conflict preflight completes before any mutation;
- external commands use argument arrays and never `shell=True`;
- mutation begins only after confirmation through `/dev/tty`;
- the mutation lock has no persistent filesystem entry;
- failed external commands remain failures and include the essential diagnostic;
- completed package-manager or external-system work is reported accurately, not
  described as rolled back.

HTTPS authenticates the bootstrap. The bootstrap pins the SHA-256 of the immutable
release archive and member manifest, rejects unexpected members and links, and stages
the executable only after validation. This is not independent code signing: control of
the site could replace both the bootstrap and its pinned hashes. See
[`SECURITY.md`](SECURITY.md).

## Runtime and release

Release 1.0.5 contains a PyInstaller one-directory application bundle for Arch x86_64.
Its project-owned CPython runtime stays inside the immutable doubt release directory;
it does not extract itself at execution time. Bundled component versions and licenses
are recorded in `COMPONENTS.json` and the archive `licenses/` directory.

Release artifacts are built twice in independent clean roots. File order, ownership,
modes, paths, and timestamps are normalized, and the two archives must be byte-identical.
Published assets are:

```text
doubt-1.0.5-x86_64.tar.gz
SHA256SUMS
COMPONENTS.json
release-members.txt
```

Release tags use exact unprefixed semantic versions and are annotated with
`doubt VERSION`. The `1.0.0` tag remains on the root commit; patch releases advance
`main` linearly. Release and Pages workflows rebuild committed bytes and never
write commits.

## Development

Development dependencies are only for contributors and release builders. The canonical
local gate is `./check`; focused gates include `unit`, `fault`, `typing`, `coverage`,
`security`, and `distribution`. Disposable Arch acceptance is run through
`./acceptance/container/run` and must not mutate the host.

See [`docs/architecture.md`](docs/architecture.md), [`docs/release.md`](docs/release.md),
and [`CONTRIBUTING.md`](CONTRIBUTING.md) for development details.

## License

Licensed under the [MIT License](LICENSE).
