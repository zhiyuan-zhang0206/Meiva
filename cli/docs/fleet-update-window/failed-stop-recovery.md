# The failed-stop recovery

The stop phase begins after all required pre-stop checks pass. A failed runner
stop in a schema-changing window aborts before stopping the gateway and recovers
runners already stopped. In a window with no pending schema migrations, a
runner's failed stop defers that runner and the window continues. A failed
gateway stop or switch also triggers recovery.

Runners stop in parallel before the gateway stops, so a failure in the stop phase
leaves the machines that already finished stopping serving nothing — and the window's
own message ("failed phase: down, nothing rolled back") describes the code, not the
outage. A failed window should cost an aborted update, not an outage: `down` therefore
ends a failed stop phase by waiting for every in-flight runner stop to settle and then
bringing back the hosts it took down.

Three facts decide one host, and none of them alone is enough: it was serving
when the window began (the preflight's own read of whether it was already stopped,
so a host an operator had already stopped stays stopped), it is down now (its `agent_host` pid is gone),
and this run is what took it down — either its stop completed here, or it is among the
hosts whose stop failed and its own `run/lifecycle-op.json` records a stop with a non-zero `rc` or
`complete != true` (written by `base/deploy/lifecycle/status_journal.py`). A
host that is still running is never touched, and a host this run cannot claim is
reported rather than started.

The start uses `up`'s own two-phase shape — gateway first when needed, then the runners in parallel, macOS through
the same one-shot `gui_oneshot` LaunchAgent — and no code is rolled back: the
switch phase never started, so those hosts still carry OLD and come back as a
usable old version. It is `meiva start` with the operator's explicit service
selection and `--start-timeout` (both required on every half), not `--boot`: a boot attempt records
its failures into the boot budget, and on macOS the failing attempt that spends
that budget exits `0` — launchd retries every nonzero exit with no attempt limit
(`base/host/system/boot_policy.py`) — so a failed recovery would read as a
success and never reach `needs manual:`. The silence stays open to its expiry or
the next `up`.

When the gateway is among the hosts to bring back and its start fails, the runners are
not started: they join the cluster through it, so each attempt could only fail on the
unreachable gateway. The log says so, and they are listed under `needs manual:`.

The half still fails with its usual exit code, and the failure output names both
lists: `recovered:` for the hosts serving again, `needs manual:` for every host
that is down and was not brought back — a start that failed, or a host this run
cannot claim. If one host's evidence cannot be read, that host is named under
`needs manual:` and recovery continues for the other hosts. The final failure
retains the original stop error and its retry instructions; a later SSH read
failure does not replace them.

## The failed gateway switch

If the gateway cannot switch to NEW the cluster cannot run NEW. Migrations run in the gateway's
own start, which `up` has not reached, so the database is still the one the old code left and the
old commit is safe to start. The window returns every host still in it to the commit its
preflight probe read (`switch_command` of that commit, idempotent), starts the hosts that were
serving gateway first (runners are not started when the gateway could not be put back), prints
`recovered:` and `needs manual:`, and exits 1.

## Contract

When a failed stop aborts `down`, it waits for in-flight stops to finish. It
starts a host again only when the host was serving at preflight, is now down,
and completed a stop in this window or recorded a failed or unfinished stop
in `run/lifecycle-op.json`. Recovery starts the gateway first when needed,
then the runners in parallel, with the one-shot LaunchAgent on macOS. It
leaves the code on OLD and the silence open, reports `recovered:` and
`needs manual:`, and exits with the stop failure.

Parent: [Fleet update window guarantees](fleet-update-window.md).
