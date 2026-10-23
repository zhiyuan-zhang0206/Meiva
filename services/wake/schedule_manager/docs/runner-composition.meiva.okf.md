---
type: doc
title: Schedule runner composition
description: Current ownership boundaries and their executable evidence.
---

# Schedule runner composition

The runner composes SDK policy inputs from one ConfigBoot and binds the schedule actor to that process's clients. Its checkout anchors launched scripts; admission rejects a development checkout against a production home before reading or executing a script. The submitted revision remains the execution authority rather than whichever script happens to be current when a stale runner wakes.

The manager receives its shell `SessionBackend`, elapsed-time `Timers`, and launch
checkout as required constructor inputs. Its daemon supplies `get_shell_backend()`,
`SYSTEM_TIMERS`, and its own repository root. Every retry-streak, failure-log and
host-generation cooldown reads the supplied timers. Recovery tests advance
`ManualTimers` while retaining the real database/request consumer, including both
a surviving predecessor and unreadable launch provenance; neither case can keep
a request queued beyond the documented bounded convergence window. PTY consumers
pass their own launch checkout instead of changing a module constant.

The runpy input-sharing test uses the actual SDK bootstrap with the runner's
configuration owner and isolated unit home. It retains the script's assertions that
the entry supplies the same producer, loaded image and machine. The separate
loader-failure case still observes actor/history/guard ordering and cleanup.
