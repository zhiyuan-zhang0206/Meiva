# Exec settlement settles on the allocation identity, not the request payload

## Context

`ExecAllocation.request_digest` was the sha256 of the request envelope file.
`validate_closed` (`agent/graph/exec/_owned_run.py`) and `_verify_closed_receipt`
(`base/agents/incarnation/exec_owner_recovery.py`) recomputed that hash before
discharging a closed allocation, and a difference raised
`ResourceEvidenceError("terminal owner request has changed")`.

The file is written by the launcher (0600) and read by the owner and the root
child. All three run as the same uid as the agent's own code, so the code an
allocation governs can rewrite the evidence that allocation is settled against.
On 2026-10-19 (company-mini, agent 6090) an exec'd cleanup script globbed
`run/exec/<agent>/domains/*/req-*.json` and rewrote its own live envelope
(811,746 -> 811,716 bytes, one 40-character secret replaced by `<REDACTED>`).
The domain had closed normally (`root_exit_code=0`, `reason=completed`); only the
post-close comparison failed.

That single refusal then had three consequences beyond the turn:

- the in-process `unresolved` entry could never be discharged, so
  `wait_retained_resources` exhausted its 5 s budget and the host restarted
  (`settlement.py`, `host.py`, `dispatcher.py`);
- recovery after the restart re-ran the same comparison and raised again, so the
  agent could not be admitted at all — a permanent per-agent wedge;
- the operator-facing event was `host_turn_stall_uncancellable`, which the same
  code path also emits for a graph that refused to unwind, so the real cause was
  indistinguishable from a stalled turn.

## Decision

Split the two facts the one digest was carrying, and version the row instead of
rewriting history:

- **`digest_version=2`** (every new allocation): `request_digest` is
  `exec_identity_digest` over fields the launcher fixes before the owner starts —
  request id, domain, agent id, generation, runtime owner, deadline.
  `request_sha256` carries the payload bytes separately.
- **`digest_version=1`** (every allocation written before this change, the model
  default): `request_digest` stays the payload sha and a mismatch still refuses.
  The `request_sha256` field is absent. Historical rows keep today's behaviour
  exactly; nothing becomes passable because it is old.
- Every payload condition is observed, not only a rewrite: `missing`,
  `unreadable`, `symlink` and `oversize` join `changed`, classified with `lstat`
  without following the entry. The evidence file and the event carry the condition,
  so an operator can tell a deleted envelope from a rewritten one.
- A payload mismatch is **always** observed: `observe_exec_payload` writes
  `<exec dir>/<agent_id>/evidence/<request>.json` (expected and observed digests,
  size, phase, whether that mismatch is authoritative) and emits one
  `exec_request_envelope_mutated` anomaly event. It refuses only when the row's
  recorded digest is the payload's (`payload_is_authoritative`).

The startup checks in `agent/execution/domain_owner.py` and
`agent/execution/owner_child.py` keep refusing *before* user code runs: they
compare the payload against `recorded_payload_sha256`, which is the same fact for
both versions.

## Consequences

- A version 2 allocation can no longer be blocked, or a host restarted, by a
  write to a file its own code controls. Version 1 rows still can — those are the
  five already-unsettled allocations on company-mini, which need the explicit
  discharge rather than a re-verification.
- Settlement no longer re-reads a 0.8-3 MB envelope on every close and on every
  admission (the read ceiling remains `MAX_PAYLOAD_BYTES` for the observation).
- **No payload condition may block a version 2 settlement.** A deleted, unreadable,
  linked or oversized envelope is exactly the failure mode a cleanup command reaches
  for, and refusing on it would move the wedge from "rewritten" to "removed".
- **Payload integrity is no longer authoritative.** This is the deliberate
  downgrade: the envelope and the code that can rewrite it share a uid, so byte
  identity was never a boundary, only a detection that fired when nobody had
  rewritten it yet. What settles a domain remains the owner receipt: exact
  allocation equality, a coherent `observed_at`, a proven-ended process and the
  transaction CAS. A reviewer who wants runtime payload authority must change the
  permission boundary so the exec'd process cannot reach the envelope; a digest
  cannot express that.
- Evidence costs one small JSON per anomaly. The payload itself is not copied:
  managed settlement never removes it, and the record carries both digests.
- Rollout: a version 2 allocation is decodable only by code at or after this
  change (`ExecAllocation` forbids extra fields). An in-flight version 1 row stays
  readable by both. Agents are admitted by one host per machine, so the exposure
  is an agent migrating machines mid-exec.

## Alternatives rejected

- **Keep one digest and treat every mismatch as a warning.** It lets a version 1
  row — including a corrupted or rotated allocation — settle without any
  authoritative check at all.
- **Hash a snapshot taken at allocation time.** The snapshot is written to the
  same uid-readable directory; it moves the race, not the boundary, and it still
  makes settlement fail closed on a file the governed code can write.
- **Quarantine every mismatch and require an operator.** Correct but not
  sufficient alone: it leaves the agent unadmittable until someone acts, which is
  the wedge this change removes.
