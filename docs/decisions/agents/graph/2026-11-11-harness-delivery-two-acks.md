# External harness delivery: two acknowledgements, observed turns, reconciliation

## Context

The graph treated an external harness as one turn at a time: reserve an input, submit it, wait for its
result, and steer mid-turn with a boolean "delivered". Three experiments on the real CLIs broke that
assumption:

- **Claude Code.** A mid-turn `query()` enters the CLI's memory queue and is absorbed only at the next
  tool boundary. When the turn has no tools, or the input arrives during the final text, around the
  end, or during an interrupt, it is not absorbed: about a second after the original result it becomes
  a **new turn of its own**. The adapter read a result as "the active ticket's", so that turn's result
  was recorded on the next submitted turn or dropped.
- **Antigravity.** An input sent after a turn ended is accepted (HTTP 200, also for a wrong field name)
  and the CLI runs a turn for it, but stdout shows nothing until the next stdin submission, then pastes
  it into that submission's `result`.
- **Codex.** A `turn/steer` whose RPC succeeded but whose `userMessage` item had not been emitted yet,
  followed by `turn/interrupt`, loses the message silently (3/3; the rollout never holds it).

The adapters answered "delivered" on a successful write or HTTP 200.

## Decision

1. **Input and run loop are decoupled.** One delivery operation, `deliver(inputs)`, hands inputs to the
   foreground; results return as events. A turn is an observed fact, not a call we made: a CLI-started
   turn (a revived queued input, a background-task completion, a timer, a monitor) is a normal turn with
   a start, activity and end, attributed correctly. We do not offer Meiva's own timers or background
   mechanism to external harnesses and do not switch off a CLI feature; a CLI-native mechanism failing
   under Meiva is our bug.
2. **Two acknowledgements, two events.** ACK 1 (`InputReceived`) says the harness process has the input,
   with a strong/weak signal. ACK 2 (`InputInContext`) says it is a message the model sees, attributed
   to a turn. A turn ending does not settle an input: only ACK 2, a rejection or a loss does. The
   frontend keeps two states (pending, in the timeline); ACK 1 is for backend decisions and
   reconciliation only.
3. **Every input carries our own id**, echoed by the protocol where it can be (Claude `uuid` with
   `replay-user-messages`, Codex `clientUserMessageId`, Antigravity `clientMessageId` found through
   the trajectory or its message-queue file).
4. **Harness durable records are a legitimate source of truth.** Where the protocol gives no receipt,
   the CLI's own record is read and matched by our id: Claude's transcript, Codex's rollout,
   Antigravity's message-queue files (not its transcript).
5. **Process generation.** Every event carries the generation of the process that produced it; frames
   from an earlier generation are dropped.
6. **Re-delivery.** After a crash or restart, an input that was received but not in the context is
   reconciled against the durable record: found means in context (Antigravity: found in the queue,
   even undelivered, means do not re-deliver, the CLI delivers it itself); not found means re-deliver in
   the original order. Only "confirmed not found" may re-deliver because no harness de-duplicates.
7. **Restart and interrupt.** Restart is an operation on the execution process, not a message: stop
   delivering, interrupt the foreground (after a 10 s grace when someone else asked), replace the
   process under the same session id, reconcile and re-deliver, then continue with a system message if a
   turn was interrupted. Interrupt reports "delivered" and "stopped" as two events and only the
   harness's terminal event ends the turn. Inputs not yet in the context after an interrupt are delivered
   as usual and become a new turn, as in the built-in runtime; there is no switch for the other policy.
8. **Scope.** Only Claude Code, Codex and Antigravity are adapted, plus the built-in runtime.

PR 1 (adapters) provides the contract, the shared ledger and the reconciliation readers; PR 2 moves the
graph and inbox onto it (one delivery path, the message state machine, re-delivery and restart); PR 3
is the frontend.

## Alternatives rejected

- *Keep "write succeeded" as delivered.* It is exactly the failure above for Claude and Antigravity.
- *Treat a harness-started turn as unsolicited output.* Loses its tools and text, and conflates it with
  the next turn we submit. Background tasks and timers are supported behaviour.
- *Rely on Antigravity `deliveryStrategy=WHEN_IDLE` to avoid phantom turns.* Tried once, never with an
  idle conversation; the interface is not public. The adapter observes the trajectory instead.
- *A per-harness special-case pile.* Shared state (key phases, attribution table, generation filter)
  lives above the adapters; they only translate frames.

## Consequences

- Every adapter must produce the unified events; the graph (PR 2) consumes events, not return values.
- Revision (graph change): `steer()`, `ForegroundSteering`, `ForegroundAccepted` and `InputLostError` are gone. The graph reads events from `HarnessBridge.subscribe()` and delivers with `deliver()`; inbox rows share one in-flight state (`claimed`) and are settled by ACK 2, a refusal or a loss. Adapters keep tickets internally for the handoff and the idle start; removing them is a later cleanup.
- The Antigravity trajectory and message-queue interfaces and the Codex rollout format are not public
  contracts; the adapters fail explicitly when a shape differs.
- CLI processes with unfinished background tasks or timers must not be idle-reaped; that gap is handled
  separately.

The assertions in [External harness adapters](2026-10-19-external-harness.md) that inbound is never
inserted mid-turn and that a mid-turn `query()` is rejected by the model are superseded by this record.
