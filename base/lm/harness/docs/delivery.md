# Harness delivery: inputs, acknowledgements, turns

The adapters (Claude Code, Codex, Antigravity) share one input and event contract. An input is
handed over with `deliver()`; everything that happens to it comes back as an event. The decision and
its evidence are in
[External harness delivery](../../../../docs/decisions/agents/graph/2026-11-11-harness-delivery-two-acks.md).

## The input

`HarnessInput(key, blocks)` (`delivery/inputs.py`). `key` is the caller's own id and names the
input in every later event. `blocks` is an ordered sequence of `TextBlock(text)` and
`ImageBlock(mime_type, data_base64)`; a wake's messages are one input, a text block per message with
that message's images right behind it, the synthetic hints first and the hook note last.
`HarnessInput.of(key, text, images)` is the single-message shape (one text block, then the images). The
type and the base64 are validated when the value is built, and a batch must have unique keys.
`HarnessInput.text` (the text blocks joined by a blank line) and `.images` are the flattened views.
What rides in front of the first message (the handoff, the compaction reminder) is added to the first
text block (`with_lead_text`). Claude Code takes the blocks as the `content` of one user message
(a single text block is sent as a plain string), Codex as the items of one `input` array; both were
recorded accepting several text blocks and alternating images with the echo (`uuid` / `clientId`)
arriving as usual (`multi_block_input.json` in each adapter's fixtures). Antigravity's stdin protocol has
one content string and takes an image only as a separate RPC, so it keeps its flattened behavior: the
text blocks joined by blank lines, the images as before (a CLI protocol limit).

Images reach all three CLIs as images: Claude as an `image` content block, Codex as an `image` input with a data URL,
Antigravity as `media` through `SendUserCascadeMessage` (an idle conversation takes the text first and the image
as a second input, see the [Antigravity notes](../antigravity/docs/antigravity.md)). The graph builds `images` from
the inlined `image_url` blocks of the message and keeps the path listing in the text. The graph bounds them: 5 MB
per image (the Anthropic API's per-image limit, the smallest of the three providers) and 15 MB of raw bytes per
input (a wake's rows are one input, so the total counts all their images; base64 makes that about 20 MB, Gemini's inline request limit); an image over a bound is not sent inline,
and the text says so.

`deliver(inputs)` returns when the batch has been handed to the foreground, in order. It never
returns "did it work": a refusal is an `InputRejected` event, a protocol failure raises. Whether an
input joins the running turn or starts one is the adapter's choice. The graph delivers with `deliver()`
and reads `subscribe()`; there is no per-input call that answers "arrived".

A batch can settle in part: the inputs the harness refused or lost go back to the queue and are handed over again, so the
timeline shows them after any later input of the same batch that did enter the context; which order the model sees
is the harness's decision and is not repaired. The handoff or compaction reminder that rides a batch's first input
counts as delivered only when that input is in the context, and is attached again to a refused or lost one.

## The events (`events.py`)

Every event carries the process `generation`: one above the last generation the session's registry record
names (1 for the first process of an agent, +1 per respawn, resume or host restart; the record keeps it as a
required field and converge completes older records with 0, so the count grows across sessions, not only inside
one object). An adapter drops a frame from an older process,
and the graph's `Foreground` drops one that arrives after a later generation began.

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
a neighbouring turn, and an input delivered meanwhile is handed to that turn or refused, never started beside it.

## Shared pieces

- `delivery/ledger.py` — `DeliveryLedger`, a pure value: key phases (they only move forward, and a
  repeated fact is silent), the provider-turn-name to local-turn-id table, exactly-once turn end,
  and the generation. Each transition returns the next ledger and the events it announces.
- `base_session.py` — owns the ledger, applies steps, and gates
  provides `_idle()` (no running turn of ours, no harness-started turn, no hold), the gate only the Codex
  adapter uses to start a turn of ours (`_deliver_one`: steer when a turn runs, else `turn/start` when idle,
  else refuse). Claude's `deliver()` writes every input to the CLI at once, whatever its turn state, and the
  echo says what became of it; Antigravity routes on the CLI's trajectory status (`route`), not on `_idle()`.
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
subscription. The graph's `harness_turn` is the one consumer: it settles each input from `InputInContext`,
`InputRejected` or `InputLost` and stays until nothing is unsettled and no turn runs
([foreground input and cancellation](../../../../agent/graph/docs/harness-foreground.md)).
Besides a turn the CLI began, the pump keeps a CLI sub-agent's activity that belongs to no turn
(`turn_id == NO_TURN`, [sub-agent text](subagent-text.md)) until the owner takes it (`take_orphans`,
`TurnEngine.take_unattached`) or a subscription reads it; it begins no turn.

## Reconciliation

After a crash the live acknowledgements are gone. Each adapter can answer, from the harness's own
durable record, `Reconciliation.IN_CONTEXT`, `PERSISTED_UNDELIVERED` or `NOT_FOUND` for a key. No
harness de-duplicates, so only `NOT_FOUND` allows a re-delivery. Reading (effect) and judging (pure)
are separate modules per adapter; the home directory is injected: the session derives it from its own
environment (`CLAUDE_CONFIG_DIR` through the auth config or `HOME/.claude`, `CODEX_HOME` or `HOME/.codex`,
`HOME` for Antigravity's `.gemini` tree) and an environment without `HOME` cannot answer. A batch of keys
is judged with one read of the record (`reconcile_inputs`); a JSONL record's half-written last line (no trailing
newline, a process killed mid-write) is skipped, a bad line anywhere else is an error. `Reconciliation.UNKNOWN`
is Antigravity's answer for an input absent from its message queue: only steered inputs are queued, an input that
started a turn on stdin leaves no file whether or not it ran.

The session answers for the session its registry record names, so no CLI has to run:
`session.reconcile(keys)` (and `HarnessBridge.reconcile`, which also answers for the session it just retired)
returns one answer per key. A session with no record, or whose record file is gone, raises
`ReconciliationUnavailableError`: only a record that exists and lacks the key may say `NOT_FOUND`, so the
caller falls back to its own explicit outcome instead of guessing. `session.adopt(keys)` (bridge `adopt`) puts
keys the harness will deliver by itself (`PERSISTED_UNDELIVERED`, Antigravity's queue) into the ledger as
received without handing them to the CLI, so the trajectory facts that follow settle them. What the graph does
with the answers is in [foreground input and cancellation](../../../../agent/graph/docs/harness-foreground.md).

## Per-adapter signals

| | ACK 1 | ACK 2 | Notes |
|---|---|---|---|
| Codex | `turn/steer` or `turn/start` response (strong) | `item/completed(userMessage, clientId)` | refusal and loss are events |
| Claude Code | the write (weak) | echo of our uuid (`replay-user-messages`) | an input that misses the turn becomes a turn of its own |
| Antigravity | stdin write or HTTP 200 (weak) | trajectory step with our `clientMessageId`, or the first stdout user-input step | an input for an idle conversation starts a turn on stdin; a late one is refused; phantom turns are observed from the trajectory |

`HarnessSession` has `deliver()` once all three implement it.
