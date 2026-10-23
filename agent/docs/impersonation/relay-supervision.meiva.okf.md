---
type: doc
title: Impersonation relay supervision
description: Existing supervision opportunities, confirmed relay exit and fenced transport replacement.
tags: [agent-lifecycle, concurrency]
---

# Impersonation relay supervision

`supervise_relay` is the native supervision seam, called from two places: the
claim gate (native loop paused or resuming) and the lease-controls pass
(services/agent_runner/agent_host/host.py `_apply_lease_controls`) while an active lease
parks the agent outside the graph — the dispatcher's pending scan wakes rows
with an open lease periodically, pull-based from the database, so supervision
does not depend on wake delivery. Executor authority is separate from relay
transport: only explicit end, native runtime replacement or original TTL
ends the lease.
Confirmed host-supervised relay exit (the host's own child handle for Codex or Antigravity) bypasses heartbeat
freshness and startup grace on the next existing supervision pass; missing or
unknown evidence is not confirmed exit. Alive senders keep the existing
heartbeat/startup grace behavior. No additional scan or wake loop is introduced.
A stale relay without this host's confirmed child-exit evidence is recorded as
degraded and is never replaced: the lease keeps its one credential and runs to
its original TTL or an explicit end, and the controller re-requests if it wants
a new relay. Confirmed exit of this host's child instead ends the lease and
returns native control on the next supervision pass. Message attempts and ACK remain durable; exhaustion
pauses a message, never identity. The ended-lease notice uses the shared
`base.agents.impersonation.host_transport` owner; native handoff does not wait for
the host's single submission.

`agent/impersonation_process.py` owns process launch: its private pipe writes the
scoped credential to the child's stdin.
The native gate supplies its machine reader to relay authentication at the
existing boundaries, after partial-context and resource-closure guards.

Transport regression tests use the existing Unix-socket Codex server fixture to
observe one real `turn/start` request across three ended-lease supervision
rounds. A server that receives the request but withholds its reply produces the
ambiguous-acceptance timeout case; the supervisor reports that failure and does
not resubmit. An explicit protocol refusal also proves that its complete error
reason reaches the failure event unchanged. A real already-reaped child exercises the retirement path. The
private-token pipe probe runs a repository helper file and retains the separate
assertion that the token never appears in the relay argv.
