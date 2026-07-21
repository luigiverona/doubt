# Failure recovery

`doubt` uses reconciliation rather than a cross-system transaction:

```text
plan -> bare mutating run -> verify -> retry
```

Successful work is kept. A failed task never triggers recursive package cleanup or a
speculative global rollback. The next plan and strict verification inspect actual
state, and a retry performs only work that still does not verify.

## Failure taxonomy

Expected operational failures are typed as blocked preconditions, unavailable
executables, command failures or interruptions, malformed output or state, unsafe
paths, permission or ownership failures, atomic-write failures, package and Flatpak
failures, remote API and authentication failures, verification drift, or concurrent
mutation. These become stable nonzero reports. Unexpected programmer exceptions remain
visible to tests and development.

## Fault evidence

The dedicated `./check fault` gate runs deterministic fixtures and real
process-handshake tests. `exit 1` below describes the application boundary; focused
task tests assert the corresponding fail result or typed exception.

| Scenario | Injection | Expected result | Mutation | Lock | Retry and verification |
| --- | --- | --- | --- | --- | --- |
| concurrent mutation | second process takes lock | exit 1, active run named | none by contender | first remains held | succeeds after first exits |
| killed mutating process | child killed while holding lock | signal exit | fixture only | kernel releases | next acquisition succeeds |
| Ctrl-C during command | fake command raises interruption | exit 130, one cancellation line | prior fixture state kept | released | later bare run can execute |
| command missing | runner reports missing executable | exit 1, executable named | none | released | install prerequisite, retry |
| package command failure | pacman, AUR, or Flatpak run fails | exit 1, source classified | desired item absent | released | retry adds item; third run is stable |
| native package conflict | installed or selected metadata conflicts | exit 1, both packages named | none | released | resolve manually outside doubt, then rerun plan |
| atomic replace failure | `os.replace` fails | exit 1, content omitted | original preserved | released | retry writes desired content |
| data fsync failure | pre-replace `fsync` fails | exit 1, content omitted | original preserved | released | retry writes desired content |
| unsafe symlink or type | managed path inspection | exit 1, unsafe path | target untouched | released | remove obstruction, retry |
| GitHub login incomplete | post-login status remains false | authentication fails | provider-defined partial state | released | status is rechecked on retry |
| Codex 02 login fails | Codex 01 succeeds, Codex 02 fails | profile 02 fails | profile 01 remains valid | released | retry logs in only account 02; final state passes |
| Codex migration collision | legacy and numeric homes both exist | verification and apply fail closed | both directories preserved | released | stop processes using the legacy home, inspect safely, quarantine the obsolete directory, then rerun |
| managed runtime downgrade | older installer sees a newer active release | downgrade is rejected before mutation | newer runtime remains active | released | use the current public installer |
| malformed TOML or JSON | controlled state fixture | exit 1, state named | invalid file preserved | released | move or correct invalid state, retry |

No test uses real package transactions, credentials, browsers, or a real user home.
Temporary homes, fake command boundaries, deterministic status sequences, and
sanitized data provide the evidence. Disposable Arch environment evidence is a
separate acceptance layer.

## Supported upgrades and downgrades

| Starting state | Direct apply | Result |
| --- | --- | --- |
| clean workstation | supported | current managed state is created after confirmation |
| 1.0.2 semantic Codex homes/launchers | supported | both profiles are preflighted, atomically renamed, then launchers are updated |
| 1.0.4 numeric state | supported | only missing or drifted managed state is reconciled |
| partial supported migration | supported when unambiguous | completed valid state is retained and missing work is retried |
| legacy and numeric home for one profile | blocked | neither directory is merged, removed, or selected automatically |
| unknown newer managed runtime | blocked | the older installer does not activate over newer state |
| downgrade to an older release | unsupported | use the current public installer; no automatic reverse migration is attempted |

The oldest directly supported release is 1.0.2. Compatibility recognition for its
exact managed profile and launcher formats remains narrowly scoped migration code; it
does not make semantic profile names current product terminology. Verification reports
migration needs and collisions without changing them. Apply performs migration only
after the single confirmation and rolls back completed profile renames when a later
rename fails safely.

## Limits

Package managers and authentication providers can fail after performing external
work. `doubt` therefore verifies after mutation and reports partial state rather than
claiming rollback. Path validation and no-follow opens protect declared managed
boundaries, but the project does not claim immunity to every race by a malicious
same-user process. See [the security policy](../SECURITY.md) for the threat boundary.
