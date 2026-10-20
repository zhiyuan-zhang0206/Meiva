---
type: doc
title: Agent Runtime Ownership
description: Hosted incarnation leases fence admission, settlement and resource cleanup.
tags: []
---

# Agent Runtime Ownership

`agents_meta.runtime_generation`, `runtime_owner`, `runtime_kind` and
`lease_expires_at` identify the admitted hosted runtime. The generation scopes an
agent incarnation; the owner identifies the host boot. A delayed task cannot
renew or settle another incarnation.

Ownership spans normal turns, idle and model-cache eviction. Idle has no active
task even when its host retains ownership. `renew_hosted_owner()` refreshes only
the current host owner's rows; `release_hosted_owner()` and
`settle_hosted_runtime()` match the original incarnation. Restart releases it for
a new generation. A fresh foreign owner blocks admission and stale-row recovery.
Every unmet closure is a
recorded refusal (`last_admission_outcome` plus a `host_admission_refused`
event), not a crash. See the shipped follow-up in
[the unowned-termination decision](../../../docs/decisions/agents/lifecycle/2026-09-29-unowned-termination-resurrects.md).
A stopping host's release is bounded (`services/agent_runner/agent_host/host.py`,
`RELEASE_OWNER_TIMEOUT_S`) so the whole stop fits meiva-root's TERM window; with
the database unreachable it fails and the leases expire by TTL.

Lease TTL and renewal ordering live in `base/deploy/progress_timeout.py` and
`base/deploy/timing.py`. The recovery sweep (`settle_stale_running_rows`) runs
at boot **and on every ownership beat**: a host that dies after boot leaves
rows whose lease only expires later, and a boot-only sweep never sees them
(2026-10-17: two rows stayed `running` for 19 minutes). The beat passes its own
owner as `exclude_owner`, so a transient renewal failure can never settle a
turn this host is still running — only an unknown or expired **other**
incarnation's rows are settled. A lease is runtime ownership evidence, not an agent identity
count: the status page counts non-terminated local identities, including idle
and maintenance-paused agents. Heartbeat selects eligible idle identities
without requiring an independently resident agent process.

## Entry points

- `agent/ownership/hosted.py` — admission, renewal, settlement and release
- `agent/ownership/hosted.py:settle_stale_running_rows` — host recovery
- `agent/ownership/hosted_claim.py` — the admission UPDATE that claims the runtime row
- `agent/ownership/corpse_reap.py` — crash-dead row termination + recovery wake; the host supplies a live wake-enabled reader, called at each original per-row transaction gate
- `agent/ownership/inbound.py` — the owner lock every inbound queue mutation takes
- `agent/ownership/hosted.py:apply_hosted_lifecycle` — executes the claimed restart/terminate command and marks it done in one transaction
- `services/agent_runner/agent_host/daemon.py` — owner health beat
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

Force terminate is not a command the host executes. The API writes
`terminated` itself in one transaction and then asks the home host to cancel the
running turn (`POST /cancel-turn`, best effort); `last_force_terminate_inbound_id`
keeps older queued input from auto-resurrecting the agent. The same route,
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

## Process fixture cleanup

`test_hosted_dead_owner_admission.py` and `test_hosted_ownership.py` use SIGKILL
for their owned look-alike host processes. A child can inherit ignored SIGTERM
from a shell session, so `terminate()` alone cannot guarantee fixture cleanup.
