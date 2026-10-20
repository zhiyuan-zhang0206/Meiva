---
type: doc
title: Agent-host graceful exit
description: SIGTERM closes admission, hands every running turn to a restart, and drains within a bound before the ordinary shutdown.
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
2. **One lifecycle command per running turn.** For each of those agents the exit
   writes a `kind='restart'`, `source='system:restart'` inbound through
   `base.agents.inbox.insert`, the same row a gateway restart writes, under the
   agent row lock a gateway restart or terminate also takes. An agent that
   already has an unfinished restart or terminate (an operator's, or the
   maintenance hold's) keeps that one and gets no second: the next host would
   apply both. An idle agent has no turn task and gets nothing. A turn task that
   has not started running, or not yet been admitted, is owed one too; its
   restart is durable, so counting it loses nothing.
3. **Drain.** The writes and the wait share one `HOST_GRACEFUL_EXIT_DRAIN_S`
   deadline, so a slow database cannot spend the stop ceiling before the turns
   are waited for. The restart takes effect where the turn is. A turn that
   reaches its next boundary first claims it there; a model call or code run in
   flight is interrupted once the restart has waited
   `RESTART_INTERRUPT_GRACE_S` (10 s), so a code run that ends sooner is not
   cut short ([[agent/graph/docs/turn-interrupts.meiva.okf.md]]). Then the graph
   ends, the checkpoint flushes, and the host applies the command: the row goes
   idling and ownership is released. If the turn was unfinished, the same
   transaction queues one pending `system_note` that continues it
   ([[agent/ownership/docs/ownership.meiva.okf.md]]); a turn that had finished
   is idle and only reads "Restart was accepted from system:restart".
   A turn that ends before its next claim leaves the restart pending instead;
   the next host claims it. `host_exit_drained` means every waited-for turn has
   ended, by either path.
4. **Shutdown.** `run` then performs its ordinary cleanup: the scheduler cancels
   whatever is left, `close_host_resources` joins resources, pools close, and
   the process exits 0.

A host that is not exiting wakes the resumed agent itself as soon as the apply
commits, through its scheduler. An exiting host has closed admission and refuses
that wake, so the next host's pending scan
(`scheduling/pending_wakes.py::scan_rows`) wakes every row with a pending inbound,
which is where the continuation note waits; the agent continues from its
checkpoint with that note as the input that starts the next model step. Nothing
new is written at boot.

## Bounds

A turn still running at the deadline is logged as `host_exit_drain_timeout`
(ERROR: agent, last turn activity, restart inbound) and falls to the cancel
path. Its restart stays pending for the next host, which takes it as an idle
agent's: the step that was cut short is lost and the turn is not resumed, as in
a crash. An agent left without a restart, by a failed write or one the deadline
cut off, is named in `host_exit_restart_failed`; its turn is still waited for.

The drain must outlast what its own restart sets in motion: the grace, the
interrupt watcher's next poll (`INTERRUPT_POLL_S`) and one
`CANCEL_UNWIND_TIMEOUT_S` to abort and flush. The clock lattice
(`base/deploy/timing.py`) requires that sum to stay below
`HOST_GRACEFUL_EXIT_DRAIN_S`.

`HOST_STOP_CEILING_S` covers the drain plus the scheduler's cancel unwind and
clock read, the resource join and room for the closes without a bound of their
own; the roster declares it as the unit's `stop_ceiling_s`, so root's TERM window
(ceiling plus `STOP_MARGIN_S`) derives from it. The lattice requires both sums to
stay strictly inside their bounds, the window inside the health round
([[services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md]]).
Root SIGKILLs a host that outlives it. A crash or SIGKILL writes no restart and
resumes nothing.

## Ordering

The generated manifest declares the agent-host first (`STOPS_FIRST` in
`meiva_root_glue/manifests.py`). Root's whole-tree stop (`all_stop_order`, used
when launchd or systemd stops root) and `meiva stop`/`meiva restart`
(`root_driver.down_units`) both go down the units in that declaration order, one
at a time, so the agent-host stops first: its draining turns still call ops and
the gateway, and the data plane stops last. Root answers `down` only once the
unit has exited. The maintenance hold
([[base/deploy/maintenance/docs/maintenance.meiva.okf.md]]) still runs before
this and drains turns itself; a host stopped after a hold has no running turn
and exits at once.

Why: [the decision](../../../../../docs/decisions/runtime/processes/2026-10-20-host-graceful-exit-writes-own-restart.md)
and [the restart that interrupts and resumes](../../../../../docs/decisions/runtime/processes/2026-10-20-restart-interrupts-after-a-grace-and-resumes-by-a-note.md).
