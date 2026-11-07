---
type: doc
title: Guarded launch retry acceptance
description: A versioned retry intent fixes one launch attempt and recovers only its existing queue wake.
tags: [gateway, agents, idempotency]
---

# Guarded launch retry acceptance

An authenticated operator submits the observed UUID `expected_prior_attempt_id`
to `POST /api/keyed/v1/agents/{agent_id}/retry-launch`. Selected-agent
`AgentSnapshot.last_launch_attempt_id` exposes this observation. The gateway
identifies the recovery by agent ID and prior attempt, including receipts
stored before that pair became the operation identity. Every request
authenticates again.
A runner that does not know the `launch-reconcile` kind rejects it before effects;
that failure never authorizes fallback to the original HTTP path or `spawn-launch`.

`ops.agents.launch_retry.accept_retry_launch` owns the transaction. It serializes
the agent and prior attempt, looks up the receipt before mutable target state, then locks
`agents_meta`. A fresh request needs the matching current attempt, idling status
and no admission observation. Stale or admitted targets return 409; missing fresh
targets return 404. The original prior/new UUIDs, placement, config overlay,
birth config and acceptance snapshot commit with the pointer rotation. Replaying the observed attempt keeps its original
acceptance after target deletion, admission or a later deliberate retry.
A deliberate new retry observes the replacement attempt.

`agent_launch_retry_receipts` has no target foreign key or expiry. Its fixed
attempt identities are never pruned or replaced by a cache. A receipt is
historical acceptance, not proof of URL/target existence, runner support, model
validation, prompt claim, host admission or a completed turn. Config snapshots
record acceptance facts; replay neither resolves changed defaults nor reverts
later operator configuration. Native host configuration/admission remains owned
by the existing host path.

`ops.lifecycle.launch_reconcile` reads the committed receipt and locks the
metadata row through `base.agents.inbox.wake.publish_inbound_wake`: only matching local machine,
current attempt, idling status and null admission may publish. Missing,
terminated, admitted, moved or superseded targets produce no wake. The publisher
uses existing Redis pub/sub; its impersonation alert
performs a plain read, without acquiring a host-owner or metadata lock. The
reconciler does not acquire a host-owner lock, so it introduces no inverse lock
order. Concurrent admission/termination/rotation waits on the same metadata row.
An already published hint may be consumed later; the existing host rechecks
placement/status, external lease and incarnation admission, and the scheduler
coalesces hints for existing durable inbound work. No prompt insertion, force
restart, resurrection or attempt rotation occurs in reconciliation.

The new kind is naturally repeatable and does not acquire an Ops transport
claim. Native crash before wake and lost RPC result acknowledgement can repeat
the hint with the same fixed attempt. Existing v2/kind canonical keys and all
uncertain Ops claims remain unchanged; this does not reinterpret an uncertain
v2 result as execution evidence. HTTP acceptance survives an unsupported or
unreachable runner, which remains a delivery gap until a later same-intent
reconciliation or the existing pending scan. No new durable worker is added.

Launch-failure responses advertise the operator route. The model-facing SDK
exposes agent creation and its accepted agent ID. Repeating a creation uses
its original receipt, independently of operator launch recovery.
