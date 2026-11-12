# Hosted Turn Cancel

Native interrupt and force terminate share one primitive: the home host's loopback
`POST /cancel-turn` (`services/agent_runner/agent_host/health_routes.py`) takes
`{"agent_id": N, "rearm": bool}`, cancels that agent's running turn Task with a bounded unwind
(`TurnScheduler.cancel_turn`) and answers `{"cancelled": true|false}`. The route
changes no row and authenticates nothing beyond the loopback port. Who calls it
owns the agent's status:

- an interrupt (`POST /api/agents/{id}/interrupt`, the stop button, `meiva agents interrupt`,
  `meiva.agents.interrupt`) leaves the row alone: the turn Task unwinds, the
  settle boundary idles the row and the next input starts a fresh turn from a
  halted checkpoint. It asks for `rearm`: a wake that arrived during the cancelled turn (the
  interrupt's own message, or, for a stop with no message, the wake of its passive note: the claim
  that wake starts takes the note into the history and idles again with no model call) starts that
  next turn at once instead of waiting for a scan, because `_pump` restarts a successor only after a completed turn. The API
  answers `interrupted` or `not_running`; an unreachable host is an error, not `not_running`, and
  the message stays queued.
- force terminate (`ops.lifecycle.terminate_agent_op`) writes `terminated` in its
  own transaction first and cancels best effort without `rearm`, so a failed cancel only means
  the turn ends at its next ownership check.

A public interrupt of an external harness writes an interrupt control row as described in
[Turn interrupts (interrupt, terminate and restart)](../../docs/turn-interrupts.md).

The scheduler owns one Task per actual turn. The host shields real awaited work,
including thread-backed work, from outer Task cancellation: a thread that cannot
be interrupted is reported by the bounded unwind instead of awaited forever.

The host explicitly supplies the original `RuntimeIncarnation` and
`HostedTurnResources` through `MeivaContext`. The scope in
`base/native_process/turn_identity.py` identifies the actual turn Task within its
service, while the reusable model cache retains no scope. Copied graph contexts
share it and the original admission reference. An exec ends before its tool call
returns (see [Tool Execution — Fault-Isolated Code Execution](exec.md)): its process group is killed, its leader
reaped, its output drained and its request files deleted, all within a bound. So
no exec outlives the turn Task that started it, and cancelling the Task ends its
exec.

The service keeps late hosted work, such as interrupt subscriptions, in one
completion span. It reports a late failure immediately without cancelling other
turns and raises it at stop/join; a delayed result never becomes a synchronous
exception in an already returned invocation. A later turn has its own scope even
when it uses the same cached model.

## Remaining boundary

A host that dies with a direct exec running does not strand that exec: its child
reads end-of-file on the parent-held control pipe and kills its own process group.
For delegated runs, exec-host closes the domain when the caller socket disconnects.
Neither daemon deletes the shared `run/exec` tree at startup, because independent
clients can still own live envelopes there. The launching client's normal cleanup
removes its exact request and result paths; abrupt client death can leave residue
without a complete automatic collector. A descendant that left the group with
`setsid` survives, as it always could.
Persistent shell sessions deliberately retain their separate ownership and are
not disposable exec descendants to kill wholesale.

## Contract verification

The cancel-route tests (`turn_dispatcher/test_cancel_turn_route.py`) cover the
route and the scheduler; the lifecycle tests cover the force and stop paths
against a real database. History owner tests verify that an incomplete
settled-write scan propagates its failure; host reconciliation preserves the
claimed row when the public committed-id resolver fails.
