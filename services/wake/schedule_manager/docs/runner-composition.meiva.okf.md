---
type: doc
title: Schedule runner composition
description: Current ownership boundaries and their executable evidence.
---

# Schedule runner composition

The runner composes SDK policy inputs from one ConfigBoot and binds the schedule actor to that process's clients. Its checkout anchors launched scripts; admission rejects a development checkout against a production home before reading or executing a script. The submitted revision remains the execution authority rather than whichever script happens to be current when a stale runner wakes.

The manager receives its shell `SessionBackend`, launch checkout, home, session
environment and `ManagerPolicy` as required constructor inputs; its daemon reads
them once (`get_shell_backend()`, its repository root, `forward_env_dict()` and
the gateway configuration) and never reads settings or the environment again. The
clock is a value: the loop passes a monotonic reading as `now` to `reconcile` and
`sync_one`. What the manager decides from it is a pure function in a strict
module: `convergence` (which action converges a schedule, what a surviving
session means for a launch, whether a replaced host may be reclaimed),
`blocked_streak` (when a failing convergence is given up and when its failure is
logged again), `recovery_gate` (when a refused relaunch is reported). The tests
of those modules pass numbers and assert the returned plan; the manager's own
tests run the real database and request consumer over a fake `SessionBackend` and
advance `now`, including both a surviving predecessor and unreadable launch
provenance; neither case can keep a request queued beyond the documented bounded
convergence window. PTY consumers pass their own launch checkout instead of
changing a module constant.

The runpy input-sharing test uses the actual SDK bootstrap with the runner's
configuration owner and isolated unit home. It retains the script's assertions that
the entry supplies the same producer, loaded image and machine. The separate
loader-failure case still observes actor/history/guard ordering and cleanup.
