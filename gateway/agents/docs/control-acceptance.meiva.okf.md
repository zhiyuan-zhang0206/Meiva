---
type: doc
title: Cancel and Compact Requests
description: Two unkeyed routes ask the host to cancel a running turn or to compact at the next turn.
tags: [gateway, agents]
---

# Cancel and compact requests

`POST /api/agents/{agent_id}/cancel` forwards to the agent's home runner.
For native Meiva, it answers `cancelled` after the host cancels the turn
Task. For an external harness, it answers `enqueued` after recording a
foreground cancel. The adapter's result settles that turn. See
[[agent/graph/docs/turn-interrupts.meiva.okf.md]].
`not_running` means there was no running turn. An unreachable home runner
is an error. Repeated clicks share an external request while it is pending.
A later request can cancel a later turn, so the route takes no
`Idempotency-Key`.

`POST /api/agents/{agent_id}/compact` inserts one `compact_request` inbound and
wakes the host; the agent summarizes and replaces its history when its claim
node next dispatches the request (`agent/graph/claim/_dispatch.py`). The
response (`inbound_id`) says the request is durable, not that a summary was
applied. A terminated agent returns 409 and nothing resurrects it for a
compaction. A repeat queues another request.

Callers: the web stop button and compact action, `meiva agents cancel` and
`compact`, and `meiva.agents.cancel` / `meiva.agents.compact`. The compact
envelope insertion primitive (`base/agents/inbox/compact.py`) is shared with
the graph and history producers. Historical `agent_control_receipts` data has no
remaining writer; merged migrations and stored evidence are retained.
