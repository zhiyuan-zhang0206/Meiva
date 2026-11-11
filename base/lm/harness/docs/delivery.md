# Harness delivery: inputs, acknowledgements, turns

The adapters (Claude Code, Codex, Antigravity) share one input and event contract. An input is
handed over with `deliver()`; everything that happens to it comes back as an event. The decision and
its evidence are in
[External harness delivery](../../../../docs/decisions/agents/graph/2026-11-11-harness-delivery-two-acks.md).

## The input

`HarnessInput(key, text, images)` (`delivery/inputs.py`). `key` is the caller's own id and names the
input in every later event. An image is an `ImageBlock(mime_type, data_base64)`; the type and the
base64 are validated when the value is built, and a batch must have unique keys.

`deliver(inputs)` returns when the batch has been handed to the foreground, in order. It never
returns "did it work": a refusal is an `InputRejected` event, a protocol failure raises. Whether an
input joins the running turn or starts one is the adapter's choice. `submit()` and `steer()` remain
for the graph as thin wrappers: `steer()` now answers with the fact: True when the input entered the model's context (also when
it became a turn of its own, which Claude does for an input that missed the last tool boundary),
False on a rejection, `InputLostError` when the harness declared it lost (both can go back to the queue); any other error means the session failed and the outcome is unknown.

## The events (`events.py`)

Every event carries the process `generation` (1 for the first process a session starts, +1 per
respawn). An adapter drops a frame from an older process.

| Event | Meaning |
|---|---|
| `InputReceived(key, signal)` | ACK 1: the harness has it. `AckSignal.STRONG` is a protocol receipt; `WEAK` is only that the write or HTTP call succeeded. |
| `InputInContext(key, turn_id)` | ACK 2: the model can see it, attributed to a turn. |
| `InputRejected(key, reason)` | The harness refused it (Codex `no active turn to steer`); it can go back to the queue. |
| `InputLost(key)` | The turn ended with no completion event for it (Codex). Safe to hand over again. |
| `TurnStarted(turn_id, origin, keys, cause, detail)` | A turn began. `origin=INPUT` names the keys that caused it. `origin=HARNESS` is a turn the CLI began on its own; `cause` is `QUEUED_INPUT_REVIVED`, `BACKGROUND_TASK`, `SCHEDULED_WAKE` or `UNKNOWN`. |
| `TurnCompleted(turn_id, ..., generation)` | The turn ended; exactly one per turn. |
| `InterruptConfirmed(turn_id)` | The harness reported the turn interrupted. |

A turn ending does not settle an input: only `InputInContext`, `InputRejected` or `InputLost` does.
A harness-started turn is a first-class turn: its text, tools and end are attributed to it, never to
a neighbouring turn, and the next turn the graph submits waits until it ends.

## Shared pieces

- `delivery/ledger.py` — `DeliveryLedger`, a pure value: key phases (they only move forward, and a
  repeated fact is silent), the provider-turn-name to local-turn-id table, exactly-once turn end,
  and the generation. Each transition returns the next ledger and the events it announces.
- `base_session.py` — owns the ledger, applies steps, resolves the `steer()` waiters, and gates
  turn starts on `_idle()` (no ticket, no harness-started turn, no hold).
- Adapters only translate protocol frames to ledger calls.

## The bridge's event pump

`pump.py` — `EventPump` is the one reader of a session's event stream, and it lives as long as the session
(the bridge starts it in `ensure()` and stops it when it retires the session), not as long as a turn. It
hands every event to the bridge's own state (model name, registration, the context-compacted flag) and
to every current `Subscription`; a subscription sees only what the pump reads after it subscribed, so a
waiter subscribes before it hands anything to the session, and an event nobody waits for is read and
dropped instead of queueing inside the session. `HarnessBridge.subscribe()` and
`HarnessBridge.deliver(inputs)` are the engine's delivery interface: `deliver` ensures the session and puts the
handoff or compaction reminder on the batch's first input; results come back as events on a
subscription. `run_turn()` is built on the same pump (it still submits through the adapter).

## Reconciliation

After a crash the live acknowledgements are gone. Each adapter can answer, from the harness's own
durable record, `Reconciliation.IN_CONTEXT`, `PERSISTED_UNDELIVERED` or `NOT_FOUND` for a key. No
harness de-duplicates, so only `NOT_FOUND` allows a re-delivery. Reading (effect) and judging (pure)
are separate modules per adapter; the home directory is injected.

## Per-adapter signals

| | ACK 1 | ACK 2 | Notes |
|---|---|---|---|
| Codex | `turn/steer` or `turn/start` response (strong) | `item/completed(userMessage, clientId)` | refusal and loss are events |
| Claude Code | the write (weak) | echo of our uuid (`replay-user-messages`) | an input that misses the turn becomes a turn of its own |
| Antigravity | stdin write or HTTP 200 (weak) | trajectory step with our `clientMessageId`, or the first stdout user-input step | an input for an idle conversation starts a turn on stdin; a late one is refused; phantom turns are observed from the trajectory |

`HarnessSession` has `deliver()` once all three implement it.
