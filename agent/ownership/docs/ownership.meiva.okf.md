---
type: doc
title: Agent Runtime Ownership
description: One agent host per machine admits turns with a single UPDATE; restart and terminate execute in one transaction.
tags: []
---

# Agent Runtime Ownership

`agents_meta.runtime_generation` and `runtime_owner` identify the admitted hosted
runtime. The generation scopes an agent incarnation; the owner identifies the host
boot. A delayed task of an older incarnation cannot settle a newer one, because
every write compares both.

The protection is the single host: root holds one agent-host per machine and home
(a flock and a stop-before-start order), and the daemon takes its own flock, so no
second host admits the same agent. An agent's `machine` is written at birth and the
host only admits rows of its own machine. There is no per-agent lease, no resource
evidence, no predecessor proof and no refusal record.

## Admission

`admit_hosted_runtime()` is the circuit breaker, the maintenance-hold check and one
`UPDATE` that claims this machine's `running` or `idling` row. It sets `running`,
the host boot as `runtime_owner` and `last_admission_at`; the generation survives
while the same boot admits again and is minted (`gen_random_uuid()`) otherwise, so
a new boot, or a restart that cleared the identity, starts a new incarnation. The
only refusals are a terminated agent, a row of another machine, an open breaker and
a hold on an agent with no unfinished restart/terminate command. A held unit lets
exactly a row with such a command through (an `EXISTS` inside the admission
`UPDATE`), whichever boot owns it; an executed restart leaves the row unowned and
unadmitted: a host that
crashed during a drain restarts, admits the member again and runs the command.

The host settles its machine's `running` rows to `idling` once, when it starts
(`settle_running_rows_at_start`): no turn survives the previous host. The beat
terminates crash-marked idling rows past `CORPSE_REAP_GRACE_S`
(`corpse_reap.py`); `last_turn_fatal_at` is the positive death signal.

Restart releases the identity for a new generation. `settle_hosted_runtime()`
matches the original incarnation.

## Entry points

- `agent/ownership/hosted.py` — admission, settlement, lifecycle apply, the start-up settle
- `agent/ownership/corpse_reap.py` — crash-dead row termination; scope is the machine, idling, past the grace
- `agent/ownership/inbound.py` — the owner lock every inbound queue mutation takes
- `agent/ownership/hosted.py:apply_hosted_lifecycle` — executes the claimed restart/terminate command and marks it done in one transaction
- `services/agent_runner/agent_host/daemon.py` — the host beat (liveness and the corpse reaper)
- `base/native_process/runtime_incarnation.py` — context-bound execution identity

Related: [[../../startup/docs/admission.meiva.okf.md]] and [[../../docs/lifecycle.meiva.okf.md]].

## Lifecycle commands

A restart or terminate command is an `inbound_messages` row that moves
`pending -> claimed -> done`. The claim node claims the oldest open command at a
turn boundary (`agent.db.claim_inbound_batch`, returned alone) and the graph
ends the turn. `apply_hosted_lifecycle` then fences on the incarnation's
generation and owner, executes the command and marks it done in **one
transaction**: restart idles the row and releases its runtime identity, so the
next admission mints a new generation; terminate writes `terminated`, keeps the
identity and closes every open terminate of the agent. No pointer, target,
applied or observed stage exists, so no command can be left half applied. A
command claimed by a host that died before applying is claimed again by the
replacement. Resurrection closes every older open command as superseded, so a
restart or terminate queued in the previous life cannot act on the next.

A restart that ends an unfinished turn also queues the note that continues it. The
caller of `apply_hosted_lifecycle` states `resume_turn` (the claim found the agent
mid-turn, not idle; see [[agent/graph/docs/turn-interrupts.meiva.okf.md]]), and the
restart's transaction then inserts one pending `system_note` with
`note_tag='lifecycle_restart'` and the restart's own source, so a restart is never
done without the note that wakes the agent through the pending scan. Idle agents,
terminates and the held-control path (no graph ran) queue none.

Force terminate is not a command the host executes. The API writes
`terminated` itself in one transaction and then asks the home host to cancel the
running turn (`POST /cancel-turn`, best effort); the force's own terminate command
keeps older queued input from auto-resurrecting the agent, since an automatic
resurrect needs a trigger newer than every terminate command of the agent. The same route,
without the status write, is the stop button: `POST /api/agents/{id}/cancel`.

A runtime generation is not a graph work identity: a cancelled turn leaves its
checkpoint mid-turn, and the next invocation starts halted and waits for fresh
input instead of continuing it.

Requested PTY cleanup runs after lifecycle commit. Graceful apply and force
terminate capture the next monotonic shell ID under the agent row lock.
Each cleanup attempts only listed IDs below its cutoff, excluding page sessions,
through the existing backend identity checks. It never holds the lifecycle lock
across PTY calls or chases later allocations. Errors stay visible without rolling
back termination; crash leftovers and late sessions require operator cleanup.
Graceful post-commit notification, browser-page release and requested shell cleanup
are independently attempted. A cancellation during browser release cannot skip shell
cleanup; the first failure is raised after the attempts, with later failures attached
as notes. Ordinary browser-release errors remain best-effort warnings.
See
[the cleanup decision](../../../docs/decisions/runtime/processes/2026-10-10-termination-pty-cleanup-after-commit.md).
