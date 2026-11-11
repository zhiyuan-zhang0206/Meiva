# Foreground input and cancellation

One path hands inbox rows to an external harness, idle or mid-turn. The head of the inbox (chat, system
notes and heartbeats, contiguous, stopping before the first row of any other kind) is reserved as `claimed`,
rendered, and handed to the engine with `deliver()` after the invocation has subscribed to the session's
events. Whether the harness starts a turn for it or the running turn absorbs it is the harness's decision; the
graph learns what happened only from events. Control kinds (restart, terminate, cancel, resurrect, fork,
compaction) are barriers: nothing behind one is delivered until the claim has dispatched it, and nothing is
delivered at all while a cancel, restart or terminate is pending.

A row has two states in front of the user, and only two. `claimed` is the pending strip: the harness was handed
the message and has not shown it to the model. When the harness reports it in the model's context (ACK 2,
`InputInContext`), the row becomes done and its timeline entries are written in the same transaction; the
frontend event for it follows the commit. The first acknowledgement (`InputReceived`) is a backend fact and
changes nothing visible. A turn ending settles nothing: only ACK 2, a refusal (`InputRejected`) or a loss
(`InputLost`) does. A refused or lost row returns to `pending` with no claim time, so it keeps its original
position and is handed over again before anything newer, with the same key and in the same order; the retry
waits for the next inbound wake or the fallback tick and never before a short backoff, so a harness that
refuses input at a turn boundary is not asked in a loop. Settlement is per input: a batch may be partly in
context and partly returned.

The graph invocation (`harness_turn`) stays for as long as there is something to settle: it ends only when no
input is handed over and unsettled and no turn is running. Claude Code runs an input that missed the last
tool boundary as a turn of its own about a second after the original turn, so the invocation is still there
when that input's ACK 2 arrives; there is no window in which an acknowledgement has nobody to write it. New
inbound reaches a running invocation through the host's wake (`TurnScheduler` hands the wake of an agent whose
task is running to its engine), with `DELIVERY_FALLBACK_SECONDS` as a bound on a lost wake; the queue is not
polled while a turn runs.

Inputs without an inbox row (a restart or resurrect note, a fork marker, a continuation after a context
reset, pre-turn hook notes, a visibility or delivery correction) are synthetic inputs in `harness_inputs`.
Each has a stable key, goes through the same `deliver()`, and writes its timeline entries at ACK 2. A fork's
first input is appended to the inherited conversation and never repeats the handoff.

A batch the session could not take at all (it failed to start, so nothing was handed over) goes back to the queue
untouched. A control row only blocks the rows queued behind it, by the same (created_at, id) order the claim uses,
so a message followed by a stop is delivered first and the stop is taken when it is out of the way. Any other
failure of the invocation first gives every input handed over an explicit outcome (the marker below) and then
raises.

An input whose delivery outcome is unknown because the session failed (the write raised, the process died) is
consumed with an uncertainty marker in the timeline and is never sent a second time; reconciling it against
the harness's durable record and re-delivering the ones that are not there is the restart-and-crash step of
[the delivery decision](../../../docs/decisions/agents/graph/2026-11-11-harness-delivery-two-acks.md), and
`retire_unconfirmed` in `claim/harness_inbox.py` is its single entry point. Rows claimed for an external
harness are not consumed by a hosted turn's settlement; only an abort or a rebuilt runtime consumes them.

Cancel targets the foreground. The graph continues reading activity until the
executor reports the terminal outcome. A normal finish that races a cancel
remains a normal finish. The cancel request does not retire a healthy session
or stop independent background work. A provider that lacks scoped cancellation
reports it as unavailable and the foreground continues. Restart and terminate
remain separate lifecycle commands and can still interrupt a graph invocation.

Parent: [Harness turn](harness-turn.md).
