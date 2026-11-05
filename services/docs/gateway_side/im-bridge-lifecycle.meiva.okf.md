---
type: doc
title: IM Bridge task lifecycle and failures
description: "Service-owned IM tasks, declared network retries and strict local state restoration."
tags: []
---

# IM Bridge task lifecycle and failures

Owner: [[im_bridge]].


## Service lifecycle

The daemon's entered `asyncio.TaskGroup` owns subscription, typing and the
configured adapter pollers. Unexpected child errors fail the
daemon; supervision may restart it. The group drains children before adapters,
health and the shared database pool close. Daemon main retains bounded cleanup
and a hard exit without joining the executor.

Teardown attempts every adapter, health server, database pool and pidfile even
after a stop fault. Unknown cleanup faults are reported with the original body
fault in a standard exception group; they do not turn failure into success.

Startup restores account-bound selections and derived subscriptions before the
bridge reports ready; `meiva start` observes readiness only after this recovery
completes. Restoring a subscription does not admit or send work. Unexpected
restoration faults fail startup.

Only explicit network failures and HTTP 429/502/503/504 retain their existing
retry budgets. Authentication, configuration, JSON/schema and programming errors propagate.

A chat message whose keyed gateway send exhausts its bounded retries is not
persisted or replayed: the gateway may or may not have accepted it, and the sender is told delivery is unconfirmed. Current
switch/filter files are strict. Retired channel records remain in the database and are not dispatched.

Immediate sends suppress resend only for a declared uncertain outcome: a known
acknowledged prefix or response lost in transport. Unknown program faults remain
visible even after an external side effect; restart may redeliver in that case.
This does not promise exactly-once delivery. Native outbox receipts are unchanged.

## Process composition

The executable captures one loaded image and retains its `CodeVersion` and
`ProcessDbGate`. The work database and lazy telemetry producer share that gate;
logging and health receive the same image. Adapter startup, auth digests,
extra routes, primary faults and bounded cleanup keep their existing contracts.

## Retry and cursor test premises

`services/entrypoints/im_bridge/tests/im_bridge_core/test_send_message_retries_through_gateway_rollout.py`
uses fake adapters/gateways and HTTP mock transport; it does not perform a live
gateway rollout or real IM delivery. Zero jitter makes the configured retry
delay deterministic. The SSE regression injects a first-stream failure and
checks that logging leaves the subscription waiting to reconnect; it stops
before proving a successful second connection.

The numeric watermark case crosses `9.5` to `10.1` without `created_at` but
supplies stable source message/block identities. It tests position ordering
under those conditions, not permission to accept unqualified legacy sources.

For adapters supporting typing, one loop per chat refreshes the indicator until
a reply is accepted or its time limit expires. Other adapters receive no typing
calls. Disabled channels are excluded when restoring subscriptions.

Button-capable channels render `/list` as a one-line header with agent buttons
and `/commands` as command buttons; plain channels keep text lists. Command
descriptions are shortened for display. Tests check the rendered output, not a
platform's ability to render a particular punctuation character.
