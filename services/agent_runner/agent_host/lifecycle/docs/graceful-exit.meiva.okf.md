---
type: doc
title: Agent-host graceful exit
description: SIGTERM closes admission, gives every running turn a durable restart, and drains within a bound before the ordinary shutdown.
status: current
---

# Agent-host graceful exit

`lifecycle/graceful_exit.py` is what a SIGTERM does to an agent-host whose
dispatcher is serving. Before that point (boot) and for SIGINT, the ordinary
KeyboardInterrupt route of `base/daemon/shutdown.py` still applies.
`sigterm_requests_exit` hands SIGTERM to the exit as a loop callback for the
duration of `serve_until_exit`; on leaving, a SIGTERM that arrived leaves the
signal ignored, so a retry cannot abort the exit it started.

## Steps

1. **Close admission.** `TurnScheduler.stop_admission` refuses every later wake,
   stops a finished turn from looping into another, and returns the agents whose
   turn task still runs. The dispatcher and its sibling loops are cancelled.
   A refused wake loses nothing: its inbound stays pending in Postgres.
2. **One restart per running turn.** For each of those agents the exit writes a
   `kind='restart'`, `source='system:restart'` inbound through
   `base.agents.inbox.insert`, the same row a gateway restart writes. An idle
   agent has no turn task and gets nothing. A turn not yet admitted is owed one
   too: its admission may observe an older restart pointer, and only a fresh
   restart leaves a pointer for the next host.
3. **Drain.** The exit waits up to `HOST_GRACEFUL_EXIT_DRAIN_S` for those turn
   tasks. The claim node takes the restart at its next boundary (before any
   model step for a turn that has not reached one), the graph ends, the
   checkpoint flushes, and the host applies the command: the row goes idling,
   ownership is released, and `agents_meta.lifecycle_command_id` keeps the
   applied restart. The agent sees "Restart was accepted from system:restart".
4. **Shutdown.** `run` then performs its ordinary cleanup: the scheduler cancels
   whatever is left, `close_host_resources` joins resources and releases owners,
   pools close, and the process exits 0.

The next host's pending scan (`scheduling/pending_wakes.py::scan_rows`) wakes every
row whose pointer is set or whose restart is still pending; the successor's
admission observes the applied restart and the agent continues from its
checkpoint. Nothing new is written at boot.

## Bounds

A turn still running at the deadline is logged as `host_exit_drain_timeout`
(ERROR: agent, last turn activity, restart inbound) and falls to the cancel
path. Its restart is already durable, so the next host still wakes it; what is
lost is the step that was cut short, as in a crash. A restart that cannot be
written is `host_exit_restart_failed`; that turn is still waited for.

`HOST_STOP_CEILING_S` covers the drain plus the cancel unwind, clock read,
resource join and ownership release; the roster declares it as the unit's
`stop_ceiling_s`, so root's TERM window derives from it and stays inside the
health round ([[services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md]]).
Root SIGKILLs a host that outlives it. A crash or SIGKILL writes no restart:
the crash path never revives an expired turn.

## Ordering

`meiva stop` and `meiva restart` stop root units one at a time with the
agent-host first (`root_driver.unit_stop_order`): its draining turns still call
ops and the gateway, and the data plane stops last. Root answers `down` only once
the unit has exited. The maintenance hold
([[base/deploy/maintenance/docs/maintenance.meiva.okf.md]]) still runs before
this and drains turns itself; a host stopped after a hold has no running turn
and exits at once.

Why: [the decision](../../../../../docs/decisions/runtime/processes/2026-10-20-host-graceful-exit-writes-own-restart.md).
