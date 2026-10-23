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
(`owned_watch`) and composes them in `subscribe_interrupt`.

`owned_watch` takes the watcher body as a coroutine the caller builds from the same event and
stop flag, so a watcher that fails, wedges or swallows its cancellation differently from the
database poll is a coroutine a test supplies, not a patched name. The package's tests drive the
ownership rules that way against a real `HostedServiceResources`, the poll loop against the
throwaway Postgres (a pool whose single connection the test holds parks the poll while `stop`
is set), and the verdicts directly.
