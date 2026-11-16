# Schedule runner composition

The runner composes SDK policy inputs from one ConfigBoot and binds the schedule actor to that process's clients. Its checkout anchors launched scripts; admission rejects a development checkout against a production home before reading or executing a script.

Declarative admission materializes `run.py` in the schedule's owner-local workspace
and starts the static runner module in a dedicated process group. Before releasing its
one-byte launch gate, the manager records the run ID, group leader's native birth, PGID,
and deadline beside the workspace payload. The payload child stays in that group. The
single-round wrapper owns prompt timeout and signal propagation; a replacement manager
uses the same claim to enforce timeout or cancellation after a crash. A run remains open
until the group is provably empty, so loss of the wrapper cannot orphan a payload or
release single-flight early. The manager, rather than user code, owns the durable run and
timing transition.

Compatibility daemon admission retains submitted revision authority rather than
executing whichever script happens to be current when a stale runner wakes.

The manager receives its shell `SessionBackend`, process-control port, launch checkout,
home, session environment and `ManagerPolicy` as required constructor inputs; its daemon reads
them once (`get_shell_backend(home=...)`, its repository root, `forward_env_dict()` and
the gateway configuration). The
clock is a value: the loop passes a monotonic reading as `now` to `reconcile` and
`sync_one`. Compatibility-runner liveness follows the same shape as a round beat: a file
under the manager's home (`base.daemon.schedules.claim`) rather than a constructor input,
so a test writes a record for a process it starts and kills instead of faking the process
table. Declarative run custody is run-specific and workspace-local; tests launch real
processes and use the process-control port only for denied-signal faults. What the manager decides from it is a pure function in a strict
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
