---
type: doc
title: Hosted Turn Cancel
description: The host cancels one agent's running turn on request; an exec ends before its tool call returns.
tags: []
---

# Hosted Turn Cancel

Stop and force terminate share one primitive: the home host's loopback
`POST /cancel-turn` (`services/agent_runner/agent_host/health_routes.py`) takes
`{"agent_id": N}`, cancels that agent's running turn Task with a bounded unwind
(`TurnScheduler.cancel_turn`) and answers `{"cancelled": true|false}`. The route
changes no row and authenticates nothing beyond the loopback port. Who calls it
owns the agent's status:

- the stop button (`POST /api/agents/{id}/cancel`, `meiva agents cancel`,
  `meiva.agents.cancel`) leaves the row alone: the turn Task unwinds, the
  settle boundary idles the row and the next input starts a fresh turn from a
  halted checkpoint. The API answers `cancelled` or `not_running`; an
  unreachable host is an error, not `not_running`.
- force terminate (`ops.lifecycle.terminate_agent_op`) writes `terminated` in its
  own transaction first and cancels best effort, so a failed cancel only means
  the turn ends at its next ownership check.

The scheduler owns one Task per actual turn. The host shields real awaited work,
including thread-backed work, from outer Task cancellation: a thread that cannot
be interrupted is reported by the bounded unwind instead of awaited forever.

The host explicitly supplies the original `RuntimeIncarnation` and
`HostedTurnResources` through `MeivaContext`. The scope in
`base/native_process/turn_identity.py` identifies the actual turn Task within its
service, while the reusable model cache retains no scope. Copied graph contexts
share it and the original admission reference. An exec ends before its tool call
returns (see [[exec.meiva.okf.md]]): its process group is killed, its leader
reaped, its output drained and its request files deleted, all within a bound. So
no exec outlives the turn Task that started it, and cancelling the Task ends its
exec.

The service keeps late hosted work, such as interrupt subscriptions, in one
completion span. It reports a late failure immediately without cancelling other
turns and raises it at stop/join; a delayed result never becomes a synchronous
exception in an already returned invocation. A later turn has its own scope even
when it uses the same cached model.

## Remaining boundary

A host that dies with an exec running does not strand that exec. The child holds
the read end of a pipe whose write end the host kept, reads end-of-file when the
host disappears, and kills its own process group. A restarted agent host also
empties `run/exec` before it serves. A descendant that left the group with
`setsid` survives, as it always could.
Persistent shell sessions deliberately retain their separate ownership and are
not disposable exec descendants to kill wholesale.

## Contract verification

The cancel-route tests (`turn_dispatcher/test_cancel_turn_route.py`) cover the
route and the scheduler; the lifecycle tests cover the force and stop paths
against a real database. History owner tests verify that an incomplete
settled-write scan propagates its failure; host reconciliation preserves the
claimed row when the public committed-id resolver fails.
