# Interrupt and compact requests

`POST /api/agents/{agent_id}/interrupt` (body `source`, `message`) commits first and interrupts after.
One transaction on the gateway writes a note that the turn was interrupted by `source`, the `message`
as an ordinary chat from `source` and, for an external harness, the interrupt control row; the
`message` is required unless `source` is `user` (the stop button). Only after the commit is the turn
interrupted: for native Meiva the gateway asks the home runner to cancel the turn Task, for an
external harness the foreground watcher reacts to the control row and the adapter's result settles
that turn. See
[Turn interrupts (interrupt, terminate and restart)](../../../agent/graph/docs/turn-interrupts.md).
`interrupted` means a turn was running; `not_running` means there was none and the message was
delivered like any chat. An unreachable home runner is an error with the message still queued. A
later request can interrupt a later turn and queues its message again, so the route takes no
`Idempotency-Key`.

`POST /api/agents/{agent_id}/compact` inserts one `compact_request` inbound and
wakes the host; the agent summarizes and replaces its history when its claim
node next dispatches the request (`agent/graph/claim/_dispatch.py`). The
response (`inbound_id`) says the request is durable, not that a summary was
applied. A terminated agent returns 409 and nothing resurrects it for a
compaction. A repeat queues another request.

Callers: the web stop button and compact action, `meiva agents interrupt` and
`compact`, and `meiva.agents.interrupt`. There is no SDK entry for the compact
request: an agent compacts itself with `meiva.self.compact(summary)`. The compact
envelope insertion primitive (`base/agents/inbox/compact.py`) is shared with
the graph and history producers. Historical `agent_control_receipts` data has no
remaining writer; merged migrations and stored evidence are retained.
