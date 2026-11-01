---
type: doc
title: The failed-stop recovery
description: What a fleet update's down half does when one host's stop fails — which hosts it brings back, and which it leaves to an operator.
tags: [cli, updates, operations]
---

# The failed-stop recovery

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

The half still fails with its usual exit code, and the failure output names both
lists: `recovered:` for the hosts serving again, `needs manual:` for every host
that is down and was not brought back — a start that failed, or a host this run
cannot claim. If one host's evidence cannot be read, that host is named under
`needs manual:` and recovery continues for the other hosts. The final failure
retains the original stop error and its retry instructions; a later SSH read
failure does not replace them.

## Contract

down's stop phase stops runners in parallel and then the gateway, so a failure in it waits for in-flight stops to settle and recovers what it took down: a host that was serving at preflight and is down now is started again when its stop completed here or its run/lifecycle-op.json records a failed or unfinished stop; a host still running, one already stopped before the window, and one this run never took down are never started; the recovery uses up's two-phase start shape (gateway first when needed, then runners in parallel; macOS through the one-shot LaunchAgent), rolls no code back, leaves the silence open, prints recovered: and needs manual:, and the half still fails with its usual exit code.

Parent: [[cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md]].
