# Foreground input and interruption

One path hands inbox rows to an external harness, idle or mid-turn. The head of the inbox (chat, system
notes and heartbeats, contiguous, stopping before the first row of any other kind) is reserved as `claimed`,
rendered, and handed to the engine with `deliver()` after the invocation has subscribed to the session's
events. Whether the harness starts a turn for it or the running turn absorbs it is the harness's decision; the
graph learns what happened only from events. Control kinds (restart, terminate, interrupt, resurrect, fork,
compaction) are barriers: nothing behind one is delivered until the claim has dispatched it, and nothing is
delivered at all while a restart or terminate is pending. A batch made only of passive notes is not handed
over: it waits and goes along with the next input that is not passive. That holds after the empty rows settle too: a
batch whose only row that is not passive has no text hands over just its synthetic inputs, and its passive rows
are returned to the queue to wait.

One wake's batch is one input, one harness turn, made of blocks: a text block for each synthetic input first (a
restart or resume note, a context reset, a continuation), then a text block for each row in queue order with the row's
images right behind it, then a text block for each pre-turn hook note, under the key of the first row that is not passive; the other rows are `Handed.carried` and settle, are returned or are lost together with the one acknowledgement; the synthetic inputs ride in it, and a refused or lost input is handed over again with them (a delivery the session could not
take leaves them in the checkpoint's `harness_inputs`, so the next invocation carries them too). Handing them over
one by one made the first input open the turn alone (a passive note, a hook note) and the person's message run as
a turn of its own about a second later. A wake with no row text (a restart note and a hook note, say) is one
input the same way: its synthetic inputs are merged in the same order under the first one's key, and a refused or lost one
is handed back whole. The images of the merged input are bounded together (15 MB), not per row; one that does not fit is
left out and a text block after the last row says so. When the CLI's record is read back for the timeline, the sent input expands back into the original inbound rows it carried. A batch of several rows marks them with `harness_input_key` in their
payload before it is handed over, so a later run rebuilds the group and asks the harness's record about that one
key (a requeue clears the mark). An interrupt request still pending when
an invocation starts was aimed at a turn that has ended; the invocation settles it before it subscribes, so it
cannot cut short the turn that answers the interrupt's message.

A row has two states in front of the user, and only two. `claimed` is the pending strip: the harness was handed
the message and has not shown it to the model. When the harness reports it in the model's context (ACK 2,
`InputInContext`), the row becomes done (`commit_handed`); the message appears on the timeline when the CLI's
record is read, and `ReadTimelineSink` publishes `InboundCommitted` with it. The first acknowledgement (`InputReceived`) is a backend fact and
changes nothing visible. A turn ending settles nothing: only ACK 2, a refusal (`InputRejected`) or a loss
(`InputLost`) does. A refused or lost row returns to `pending` with no claim time, so it keeps its original
position and is handed over again before anything newer, with the same key and in the same order. That
requeue is also a run boundary: the current invocation waits for its foreground turn to end and for a short
backoff, then exits without reserving the row again. Its checkpoint retains synthetic inputs that rode with
the row. The next `harness_turn` invocation makes the next delivery attempt, so a harness that refuses input
at a turn boundary is asked once per run rather than in an in-process loop. Settlement is per input (a wake's rows are one input): a delivery of several
inputs may be partly in context and partly returned.

The graph invocation (`harness_turn`) stays for as long as there is something to settle: it ends only when no
input is handed over and unsettled and no turn is running. Claude Code runs an input that missed the last
tool boundary as a turn of its own about a second after the original turn, so the invocation is still there
when that input's ACK 2 arrives; there is no window in which an acknowledgement has nobody to write it. New
inbound reaches a running invocation through the host's wake (`TurnScheduler` hands the wake of an agent whose
task is running to its engine), with `DELIVERY_FALLBACK_SECONDS` as a bound on a lost wake; the queue is not
polled while a turn runs.

A turn the CLI began itself while the agent was idle (a timer, a finished background task) enters the same
run: the host's wake finds an empty inbox, but the claim sees the engine's unpresented turn and the run's
subscription is primed with it. The run projects the turn like any other, with no line naming its cause; the invocation ends only when no input is unsettled, no turn runs and no event is waiting to be read.
The turn is presented once, by whichever run reads its start ([background work](../../../base/lm/harness/docs/background-work.md)).

Images of a message go to the harness as images (`HarnessInput.images`, built from the message's inlined
`image_url` blocks by `native_images`), with the "Attached images" path listing left in the text. Bounds and the
per-CLI delivery are in [harness delivery](../../../base/lm/harness/docs/delivery.md).

Inputs without an inbox row (a restart or resurrect note, a fork marker, a continuation after a context
reset, pre-turn hook notes, a visibility or delivery correction) are synthetic inputs in `harness_inputs`.
Each has a stable key and goes through the same `deliver()`. A fork's
first input is appended to the inherited conversation and never repeats the handoff.

A batch the session could not take at all (it failed to start, so nothing was handed over) goes back to the queue
untouched. A control row only blocks the rows queued behind it, by the same (created_at, id) order the claim uses,
so a message followed by a stop is delivered first and the stop is taken when it is out of the way. Passive deliverable rows do not block control claims: they wait for the next non-passive input. A control behind only passive notes is taken first; a resurrect input can run alone while those notes stay pending. When a non-passive deliverable precedes control, its head batch carries the passive notes before control is claimed. Any other
failure of the invocation first gives every input handed over an explicit outcome (reconciled, or consumed as unconfirmed, as below)
and then raises.

An interrupt targets the foreground. The graph continues reading activity until the
executor reports the terminal outcome. A normal finish that races an interrupt
remains a normal finish. The interrupt request does not retire a healthy session
or stop independent background work. A provider that lacks scoped cancellation
reports it as unavailable and the foreground continues. Restart and terminate
remain separate lifecycle commands and can still interrupt a graph invocation.

## Inputs handed over and never acknowledged

A failure, an interrupt, a host abort or a restart can leave rows `claimed` that the harness never reported as in
its context. Such a row is delivered again only when the harness's own durable record (Claude's transcript,
Codex's rollout, Antigravity's message-queue files) shows it is not there, and nothing is guessed when the record
cannot tell. Re-delivering a row the record already holds would not be a harmless duplicate: Claude Code drops a
user message whose uuid its session already holds, after echoing it (see [halted turns](#halted-turns) below). `bridge.reconcile(keys)` reads the record once and answers, per
input, `IN_CONTEXT`, `PERSISTED_UNDELIVERED`, `NOT_FOUND` or `UNKNOWN`; `plan_reconciliation` (pure, in
`claim/harness_foreground.py`) turns the answers into one decision per input:

| Record says | The row |
|---|---|
| `IN_CONTEXT` | `done`, like an ACK 2 (`commit_reconciled`) |
| `NOT_FOUND` | back to `pending` at its original position, delivered again in order |
| `PERSISTED_UNDELIVERED` (Antigravity's queue holds it) | stays `claimed`; the session adopts it and the harness's later facts settle it |
| `UNKNOWN` (Antigravity: no queue file for it) | consumed as unconfirmed, never sent again |
| no record exists (no session record, file gone) | consumed as unconfirmed (event `harness_reconcile_failed`) |
| a record exists but could not be read (I/O, an interface change) | stays `claimed` and the error propagates; the next wake reconciles again |

Antigravity's queue only holds inputs steered into a running turn. An input sent while the conversation was idle
starts a turn on stdin, carries no id and leaves no file (observed on a live agy: no queue directory at all after
an idle input ran), so its absence proves nothing and is `UNKNOWN`, not `NOT_FOUND`. A Claude transcript or Codex
rollout whose last line is half written (a process killed mid-write, no trailing newline) is read without that
line; a bad line anywhere else is an error.

There are two entry points, both in `ForegroundRun`:

- **A run that fails or is interrupted** (`retire_unconfirmed`). It retires the session first, so a CLI that
  still held an input in a queue of its own cannot take it later and "not found" is final; then it reconciles
  the inputs it had handed over. When the session cannot be retired the record is not asked and the inputs are
  consumed as unconfirmed.
- **The start of a run** (`_adopt_claimed`). After the session is up (resumed, with a higher generation), every
  claimed deliverable row that this run did not hand over is an orphan of an earlier process (a crash, an abort,
  a host restart) and is reconciled before anything new is delivered. The claim also enters the turn for such
  an orphan when the queue is empty, and the host's pending scan wakes an idling external agent that holds one,
  so a message is not stranded until the next one arrives.

What the agent is told. An input settled as in the context lost the turn it was in with the process, and the
harness will not continue it by itself, so a recovery input is added (`reconciled-resume-<id>`: the process was
restarted and the turn was interrupted, continue), unless another recovery input (a restart or resurrect note, a
harness switch's `context-reset-…`) is already on its way. It is also what makes an adopted Antigravity input
arrive: agy delivers its queue before the next stdin input, and the trajectory poll can only attribute that
input to a turn while one of ours is running. Once that turn is over and every other input is settled, an adopted
input that has still not appeared is asked about once more: gone means `NOT_FOUND` (back to the queue, delivered
in this run), still stored means it stays claimed and is marked `harness_reconcile_deferred`, so neither the claim
nor the pending scan wakes the agent for it again; the next run that starts reconciles it once more (and settles
it if it is in the context by then).

Order after a restart: the recovery input (`control-…`, `context-reset-…`, hook notes, `reconciled-resume-…`) is
the front of the first batch, then the rows in their original queue order, returned ones included; a pending
restart or resurrect note (`lifecycle_restart` / `lifecycle_resurrect`, written by the restart apply after the
older rows) is moved ahead of the rows in its batch (`recovery_notes_first`), so the agent learns it was restarted
before it sees the messages that are delivered again. The handoff or compaction reminder still rides the batch's
first input. An input the harness stored itself and delivers on resume is in the context before that batch, as
the harness decides. Inputs not in the context when a turn was interrupted are delivered as usual and become a
new turn.

Process generation: each frame carries the generation of the process that produced it, and the session record
keeps it (a required field; converge gives older records generation 0), so it grows across respawns, sessions
and host restarts. `Foreground` reads the bridge's generation when it starts and drops, before anything reads
it, a frame of an earlier generation (event `harness_event_stale`): turn names restart with each session and a
re-delivered input keeps its key, so a late frame of the old process must not end the new process's turn or
settle the re-delivered input. Synthetic inputs lost with a failed run (no row to return them to) are not
re-delivered.

The old CLI process. A killed host closes the CLI's stdin, and all three CLIs exit on that (observed on macOS by
SIGKILLing the host process while a Claude Code, Codex and Antigravity session was up: each CLI was gone within
four seconds, `agy`'s `start_new_session` notwithstanding). The session record keeps no pid and the host does
not wait for the old process before it resumes, so a CLI that survives its host (stuck in a tool, for instance)
is not detected; the facts above are the whole protection, and no process registry was added for it.

## Halted turns

A turn that stops before it delivered a reply halts the agent instead of failing it: the harness ran out of quota
(HTTP 429, a usage limit), or the turn stalled, no event from the harness for the agent's
`harness_turn_stall_seconds` (default 15 minutes) while an input it was handed or a turn it began was still
unsettled. Nothing in the protocol ends such a wait otherwise: the host's progress tick only says that the host
process is alive, and `harness_turn` is exempt from the node stall probe. The stall clock is the last event read
or the last handover; an event being written or waiting to be read counts as heard.

Both settle the same way (`harness_turn.py::_settle_halted_turn`, `harness_inbox.py::settle_halted_turn`): the
session is retired (a stalled CLI cannot act on the inputs later), the turn's rows, those already marked done by
an echo included, go back to `pending` with their handback counted (`harness_quota_retries` in the payload), and
the recovery breaker halts the agent in the same transaction with the reason `quota_exhausted` or `turn_stalled`.
The alert is the turn's Error event, the anomaly event (`harness_quota_exhausted` / `harness_turn_stalled`), the
`recovery halted` line of `meiva agents get`, and health-probe check 10, which counts each reason apart. A new chat
resumes the agent (`breaker.py::admit_pending_wake`); the handed-back rows ride with it. A stall or quota abort after
the turn already delivered output still settles as `HarnessTurnHaltedError` with no rows handed back
(`requeued_inbound_ids=()`): its rows are not sent twice, the timeline finishes flushing what was produced, and the
agent idles without stamping `last_turn_fatal_at` so the corpse reaper does not terminate it. A turn that calls an
explicit timed `schedule(DurationSeconds=...)` tool extends the silence clock floor to the timer's due time (capped at
24 hours) so waiting for the timer is not misclassified as a stall while silence past the due time still triggers it.

A handed-back row is handed over again under a new key, `inbound-<id>-handback-<n>`
(`base/agents/messages/harness_input_key.py`). Its first key is in the harness's record already, and Claude Code
drops a user message whose uuid (derived from the key) it already holds: it echoes the message, so the turn
opens and the row is settled as in the context, and then runs no turn and sends no result. On 2026-11-19 that
held 11 agents in a turn that never ended for 47 minutes, with every message behind the dropped one stuck
(task #7423); a row that was never handed back keeps `inbound-<id>`. The keys a row was handed over under before each handback stay
on it (`harness_handed_keys`), so the timeline still finds the row under the earlier handover the record holds.

Parent: [Harness turn](harness-turn.md).

## Timeline visibility

All external harnesses read their timelines directly from the CLI's record
([harness turn](harness-turn.md#record-read-timeline-external-harnesses)). Settling an inbox row only updates
`inbound_messages` (`commit_handed`, `commit_reconciled`); no database timeline rows are written. Synthetic
inputs that no inbox row names (a context reset, a recovery note, a visibility correction) are sent to the CLI
without appearing as standalone user rows on the timeline.
