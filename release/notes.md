# doubt 1.0.5

This hardening patch makes cancellation of package and build providers bounded and
descendant-safe. Doubt requests graceful process-group termination, waits two seconds,
then escalates and reaps without changing `KeyboardInterrupt` or command-failure
classification. Normal and verbose provider output now share bounded redaction, while
GitHub and Codex authentication retain their interactive terminal.

`doubt --version` is read-only. Supported migration collisions are visible in verify
mode without mutation, direct upgrades from 1.0.2 and later retain exact legacy-state
recognition, and older installers refuse to replace a newer managed runtime.

The patch also removes test resource leaks, corrects the documented abstract-socket
mutation lock, and records the upgrade, downgrade, and deferred progress-event design.
