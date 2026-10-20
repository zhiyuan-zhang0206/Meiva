---
type: doc
title: Incarnation resource evidence
description: Nullable server-owned resource evidence and conservative schema retirement.
---

# Incarnation resource evidence

`agents_meta.incarnation_resources` is separate from configuration. NULL means
unknown, not an empty resource set. The additive migration does not initialize
historical owners or grant resource admission. Actual admission must
establish ownership before creating a complete empty set.

An exec leaves no database evidence: nothing registers, attaches or discharges an
allocation any more (see "Exec processes" below). The stored model still carries a
`requests` map for values an earlier runtime wrote; a non-empty map refuses
successor admission and resurrection until an operator clears it by hand.

A retirement migration takes ACCESS EXCLUSIVE before checking evidence and
retains that lock through DROP. Only entirely NULL, never-enabled storage can
be removed. Every non-NULL value, including an empty map or malformed version,
refuses retirement. An empty map alone cannot prove that a live producer will
not write again. Used-state rollback therefore requires a separately verified
all-writer retirement operation; this slice provides no automatic evidence
clearing or legacy rollback permission.

Process and hosted admission preserve legacy NULL/protocol zero and refuse
malformed evidence. The metadata row owns runtime admission: once its generation,
owner, kind and per-agent PID are released, a successor may transfer the empty
resource set under the same row lock as its ownership claim. Admission does not
query historical restart or terminate receipts. A failed model turn can be reaped
and resurrected in the same live shared host; it needs no synthetic lifecycle
receipt and no host restart. Resurrection refuses a stored exec allocation
before releasing the identity.

An unreleased managed owner can be replaced only when its admission-captured
host native birth is proven ended, with an empty resource set. A live
host, an expired lease alone, a missing host identity, or any request
refuses that takeover. A legacy NULL row keeps its unknown set
(admission never mints an empty one) and is replaced only after its lease
expires; NULL evidence never authorizes an earlier takeover. Fresh `base.agents.birth.transaction.insert_agent_birth` metadata INSERTs stamp
one server-owned `ResourceBirth` in the same birth transaction. Real hosted
admission consumes it and captures its own host process before advertising
managed work. Receipt replay never refreshes resource evidence; historical NULL
rows remain unknown. An environment flag or installed revision is not evidence.

Values the current model cannot decode, the closed-predecessor form, drained
sets and never-admitted resurrection:
[[closed-predecessor.meiva.okf.md|closed predecessor]].

Hosted admission locks only the agent's own metadata row and reads no
deployment-wide state. A stored resource value the current model cannot decode
refuses under every admission (`resource_fence`). Historical NULL resource rows
remain unknown and cannot become empty through admission.

Admission runs under the least-privilege `meiva_runner` database identity, which
holds no write privilege on `deployment_state`.

## Exec processes

The host launches each exec child directly (`agent.execution.child`, see
[[agent/graph/exec/docs/exec.meiva.okf.md]]) as the leader of its own session and
process group, and kills that group before it returns. The host keeps the write end of
the child's stdin pipe until it has reaped the leader: when the host dies, the
child reads end-of-file and kills its own group. A restarted agent host empties
`run/exec` before it serves. A successor therefore needs no per-exec evidence from
the database; the facts it relies on are the predecessor host's stored
`host_process` (proven ended below) and lease expiry. Same-user arbitrary code is
not sandboxed: the guarantee is the exec's process group, not every possible detached
or breakaway process. Persistent sessions remain independent.

Every `ResourceProcess` receipt requires explicit `starttime` and `boot_id`
fields. Local Linux custody requires PID, exact kernel start ticks, and boot
UUID; its wall-derived creation timestamp is diagnostic. macOS requires its
boot UUID and exact stable kernel birth. Missing historical
native fields refuse; they cannot become evidence that a host ended. A different
recorded boot or exact native PID reuse proves the prior process ended without
authorizing work on its replacement. Failed identity observation remains unknown.
Fresh host captures compare native births; database CAS retains exact model equality,
including the original timestamp. `resources.process_ended` is the one probe that turns
a stored receipt into "this process ended". Same-incarnation admission preserves the
stored receipt rather than rewriting its facts.

Hosted admission retains the exact owner and resource fences. It does not
produce a cross-generation work-transfer certificate or recover cancellation.
