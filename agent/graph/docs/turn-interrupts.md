# Interrupting a turn: interrupt, terminate and restart

A public interrupt (`POST /api/agents/{id}/interrupt`) first commits, in one transaction, a
`lifecycle_interrupt` system note (who interrupted), the interrupter's message as an ordinary `chat`
inbound and, for an external harness, a control row of `kind='interrupt'`; only then is the turn
interrupted. The agent's first input afterwards is that note and message, in id order, so it learns
who interrupted it and why before anything else (a heartbeat can only come later). See
[the decision](../../../docs/decisions/agents/graph/2026-11-12-interrupt-carries-a-message.md).

For native Meiva turns the gateway then asks the home host to cancel the running Task. The in-flight
node unwinds, the settle boundary idles the agent, and the host starts the next turn at once from the
wake the message left (`cancel_turn(rearm=True)`). See
[Hosted Turn Cancel](../exec/docs/hosted-turn-cancel.md). A native agent gets no control row: that
durable row was removed on purpose.

For a running external harness the control row is the interrupt. The foreground watcher asks the
adapter to interrupt while it keeps reading the session. The harness's terminal event ends the turn, and the
invocation ends once everything it handed over is settled; an input that was not in the context when the
turn was interrupted is still delivered and becomes a new turn, as in the built-in runtime. The control row
is the last of the three, so claim consumes it only after the note and the message in front of it have been
delivered. Session shutdown remains a separate operation.

The web stop button has no text: it sends the same request without a message and as `user`. The agent
is left one passive note (`Your running turn was interrupted by user.`). Passive means it asks for no
answer: the agent is never made to run a model turn for it. It is nevertheless in the agent's history and
on the timeline as soon as the interrupt has taken effect:

- **Native.** The gateway publishes a wake for the note like any inbound. The host rearms the cancelled
  turn's successor, whose claim takes the note into `state.messages` (a quiet-note batch: `quiet_notes`
  in `_decide.py`) and returns to claim with `halted`, so the agent idles again with no model call. The
  row is consumed by that claim, so the next input does not carry it a second time. If the wake or the
  host is lost between the cancel and the claim, the row simply stays pending and rides in the next real
  input's claim, once, as before this change. That idle invocation is a hosted turn with no model
  call, so it admits the runtime (`status` running then idling again, `last_admission_at`, two
  `status_change` events, a turn span and one concurrency slot for a few database round trips). None
  of that reads as activity: `last_active_at`, which the heartbeat idle clock reads, is written only by a completed model turn (the
  API's last-active field reads inbound rows, which the stop's note already touched), and the claim of a passive note clears no breaker
  (admission refuses a tripped breaker before the claim). It is the accepted cost of one extra
  invocation per stop.
- **External harness.** A passive note alone is never handed to the harness (a delivery would start a turn
  for it), so the control row is written ahead of the note, where claim takes it. When the foreground
  confirms the interrupt (the harness's interrupted `TurnCompleted`), `announce_interrupt` writes the
  pending passive notes' timeline entries and flags each row `timeline_written` in one transaction
  (`write_interrupt_notes`). The row still waits for the next real input and rides in its text, which is
  how the CLI's context learns of it; its entry is not written again (`timeline_items_of`). A crash before
  that transaction leaves the row unflagged, so the entry is written when the row is delivered; a crash
  after it leaves the entry written and the flag set. Either way the note appears once.

An interrupt request still pending when an invocation starts is stale (its turn ended on its own first)
and is settled at once. An interrupt of an agent with no running turn writes no note and no control row:
its message is an ordinary chat.

Terminate remains a durable inbound. The two-second DB poll in
`agent/graph/interrupt/` (`watch_for_interrupt`, cadence `INTERRUPT_POLL_S`) observes a pending terminate row during
LLM/exec, halts and returns to claim, which routes termination to `END`. A pending restart
joins the poll after a grace, below. The `kind='interrupt'` row is an external harness's only: its claim
handler keeps the agent alive and idle, and the foreground watcher (`InterruptScope.FOREGROUND`) reads it.
A native node's watcher never meets one, because a native interrupt cancels the turn Task instead.

A terminate **yields to a message the death decision did not see**: a chat
co-batched with a self-initiated terminate (`meiva.self.terminate()` — the chat
arrived during the very turn the agent terminated in), a chat newer than an
external terminate (the latest intent wins, same recency rule as the
resurrect veto), or any pending inbound newer than the whole claimed batch (a
final queue recheck before the exit is committed). The vetoed terminate is a
consumed no-op: no marker, no `END`, never retried — the agent stays alive and
processes the message, so the last message is never swallowed by a racing
death.

An external harness's own terminate is the one self-initiated command that interrupts. The agent's
`meiva.self.terminate()` runs in a shell of the CLI, where nothing raises `LifecycleExit`, so the row would
wait for the CLI's turn to end by itself, and a turn blocked on a provider the CLI keeps retrying never
does (agent 7600, 2026-11-11: the model request after the call failed for three hours). The foreground's
lifecycle watcher (`InterruptScope.LIFECYCLE`) therefore reads the agent's own terminate once its row has
waited `RESTART_INTERRUPT_GRACE_S`: the closing words an agent writes after the call arrive within seconds,
so a normal turn ends inside the grace with nothing cut short, and one that is still running has nothing
left to say. The interrupted invocation returns to claim, which takes the terminate as for any other.
Native nodes keep excluding the agent's own row.

Node-level discard semantics differ, and deliberately so:

- **llm_node** discards the partial generation whole — nothing was committed, so
  there is nothing worth keeping.
- **exec_node** keeps partial stdout/stderr when its watcher fires and attributes the tool result to
  the first observed row: `[cancelled by user]` for a user terminate,
  `[cancelled by system]` for another source's terminate (maintenance, an external system command),
  `[interrupted by restart]` for a restart. The watcher retains that
  attribution through subprocess cleanup; a later inbound cannot relabel it.
  Existing side effects are not undone or replayed.
- A user stop, `meiva.agents.interrupt` or a host cancel (`POST /cancel-turn`) cancels the turn Task
  instead, so the running exec is killed (see [Hosted Turn Cancel](../exec/docs/hosted-turn-cancel.md))
  without writing a result. The unpaired tool call then receives the synthetic
  `[interrupted: no result was recovered ...]` result (`INTERRUPTED_TOOL_RESULT`,
  `agent/hooks/repair.py`) at the next turn's history repair, and no partial output is kept.

## A restart interrupts after a grace and resumes the turn

A pending restart (never the agent's own, `source='self'`, which already ends its
exec by raising) is an interrupt too, but only once its row has waited
`RESTART_INTERRUPT_GRACE_S` (10 s, `base/native_process/stop_timing.py`) on the
database clock; `pending_interrupt_reason` reads the row's `created_at`. Most code
runs end inside the grace and the restart then takes effect at the next turn
boundary with nothing cut short. Interrupt and terminate need no grace. The watcher's
poll (`INTERRUPT_POLL_S`) bounds how soon past the grace the node notices, and the
graceful exit's drain is held above grace, poll and one unwind by the clock lattice. The
production cadence and the five-second exit bound are the defaults of
`subscribe_interrupt(..., poll_s=, exit_timeout_s=)`; the nodes call it with those defaults,
and the package's own tests pass small values against a real database.

An interrupted node returns `interrupt_halt(reason)`: `halted`, and for a restart
also `interrupted_by_restart`. That mark exists because `halted` cannot say whether
the turn is over: an agent that finished or was paused is halted, and so is a turn a
restart cut short. The claim that takes the restart (`handle_restart`) calls the
agent idle only if it is halted without the mark, or has no history, and the
restart is not the agent's own; `meiva.self.restart()` ends its exec halted with the
turn unfinished, so it never counts as idle. After the claim `halted` is that
verdict: set, the agent was idle and reads `restart_accepted_text(source, message)`
("Restart was accepted from ..." plus `: {message}` when the restart row carried
a non-empty `content`); clear,
the turn was unfinished and claim adds no marker. The claim clears the mark, and
every invocation's input does.

The host reads the verdict when it applies the restart
(`finish_completed_invocation`: `restart_requested` and not `halted`).
`apply_hosted_lifecycle` then queues, in the transaction that executes the restart,
one pending `system_note` (`lifecycle_restart`, the restart's own source) from
`restart_resume_note(source, message)`: who asked (and the optional explanation from
the restart row's `content`), that running code was terminated with its result in
the matching tool message, that shell sessions may not have survived. Nothing else is
asserted, because the writer does not know it. Once the apply has committed, the host
wakes the agent through its own scheduler (`AgentHost.bind_wake`), so the next turn
starts when this one ends; a host that is exiting refuses the wake, and the pending scan
finds the note on the next host (it also covers a host that bound no scheduler). The
claim renders the note and the model runs from the checkpoint. The lease-control path (an active impersonation lease) runs no
graph and never queues the note; a hard kill or a cancelled drain leaves the
checkpoint's turn abandoned, since each turn's first invocation starts halted.

An accepted native cancel remains bound to its original work across action
boundaries; a later chat or work UUID cannot consume that command.

## The agent's own restart waits for the turn to end, on any harness

`meiva.self.restart` on an external harness, or from any harness with a `harness` in the overlay,
is recorded as a pending `restart` command (`source='self'`) carrying a `restart_transition`
(`base/agents/incarnation/restart_transition.py`): the target harness and the overlay to merge.
The home runner writes it after the gateway's usual validation
(`ops/lifecycle/deferred_restart.py`); nothing about the agent changes while its turn runs. The
command is never an interrupt (the watcher excludes `source='self'`, however long it waited), and
steering of messages that arrive meanwhile stops at it, so they stay pending. When the harness turn
or native execution returns, the claim takes the command first and alone and `handle_restart` ends the
invocation on every harness; the agent's own source is never idle, so the apply queues the note.
`apply_hosted_lifecycle` then stores the overlay and the harness, releases the incarnation, closes
the command and queues the note, in one transaction, and the host removes the old harness's session
record just before it (a retry may run that twice) and writes the switch note in the outgoing
history just after it. The note is `[Harness switched from 'a' to 'b']` plus the agent's message for a
switch, and the restart-accepted text for a restart that kept its harness (the session resumes).
A command still claimed when its host died is claimed again by the next incarnation and applied then;
a command still pending when the agent crashes is superseded by the resurrection.
See [the decision](../../../docs/decisions/agents/graph/2026-11-11-self-restart-at-turn-end.md).
