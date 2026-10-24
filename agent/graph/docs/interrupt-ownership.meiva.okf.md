---
type: doc
title: Interrupt ownership
description: Explicit invocation and service ownership of interrupt watcher results.
tags: []
---

# Interrupt ownership

A database-backed `subscribe_interrupt` requires explicit `HostedTurnResources`
linked to the original `HostedServiceResources`. The four production consumers
(exec, model stream, graph compaction and hosted compaction) pass the invocation's
actual context scope. A direct graph embedder supplies that scope and joins its
service before closing the interrupt pool; no process-global owner is inferred.

Subscription exit sets stop and cancels the actual watcher, then waits at most
five seconds. Cancellation wins a same-tick race against a model result. A poll
that returns after stop cannot set the old event. In-flight unknown failures
reach the caller, preserving a primary failure and cleanup secondary together.
After the finite return, the original service retains the late watcher result:
unknown failures are visible immediately and raised by service stop/join, while
other agents continue. Service join shares the host's existing unwind deadline;
uncooperative work retains actual clients/pools until its existing hard exit.

## Where each decision lives

`agent/graph/interrupt/` splits the watcher into a shell and a plan. `_plan.py` is a strict
module of values-in, verdicts-out: whether a poll result fires the event
(`watch_step`: a reason found while `stop` is set never fires), who reports a watcher failure
(`failure_action`: the service for a late one, the invocation otherwise), how a retained
watcher's fate reads (`watcher_fate`), and how a bounded exit ends (`exit_verdict`: retain or
unwound, which exception is raised, the primary failure kept with the watcher error as a note
and a grouped cause). `_watch.py` runs the poll loop (`watch_for_interrupt`), owns the task
(`_OwnedWatch`) and composes them in `subscribe_interrupt`, the only entry that starts a watcher.

The package's tests drive the poll loop and the ownership rules against the throwaway Postgres
and a real `HostedServiceResources`: a pool whose single connection the test holds parks the
poll while `stop` is set, and a pool that was never opened fails the poll in flight. The faults
a real pool cannot produce, because psycopg unwinds a cancelled acquire or query at once (a
watcher that raises while unwinding, stays wedged past the bound or swallows the cancel), come
from `agent/graph/interrupt/tests/parked_pool.py`, a stand-in for the one pool member the
watcher calls (`connection()`): its acquisition parks until cancelled and then does what its
constructor said. It is not a subclass of the psycopg pool and carries no switch. The fault
enters where the watcher meets its resource, never as a coroutine handed to the owner. The
verdicts are asserted directly. The stand-in is a stopgap for a narrow `InterruptPool`
protocol that `base/` is to host with its own fake; until then `subscribe_interrupt` keeps the
concrete pool type and the stand-in is presented as one through `as_pool`.
