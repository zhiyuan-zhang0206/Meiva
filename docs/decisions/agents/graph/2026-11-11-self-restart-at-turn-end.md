# An agent restarts itself, and switches harness, at the end of its turn

Supersedes the `restart` part of the lifecycle-self-call paragraph in §16 of
[External harness adapters](2026-10-19-external-harness.md) ("`compact`, `restart` and `terminate`
belong to the bridge that owns the session ... refused on that ground") and the matching "native
only" rows of [the capability boundary](../../../../agent/graph/docs/harness-capability-boundary.md).
`compact` and `attach` stay native only; `terminate` is unchanged.

## Context

An agent could not switch its own harness. The SDK refused `harness` in the overlay of
`meiva.self.restart`, refused `restart` outright on an external harness, and the operator route
(`POST /api/agents/{id}/restart`, `meiva agents restart`) answers 409 unless the agent is `idling` or
`terminated`. An agent that is calling the SDK is running, so every road was closed.

The harness transition cannot simply be allowed from inside the turn. It stops the agent's session
(for an external harness, the process that is making the call), force-terminates the row and
resurrects it; run from the agent's own call it would kill the caller halfway through the sequence.
The validation that makes a switch safe (the target harness launches on the machine, an explicit
model for it, the overlay and the model) lives in the gateway and is answered per request.

## Decision

`meiva.self.restart(message, config_overlay=...)` means the same thing on every harness: record a
durable restart request now, let the running turn end, apply it at the boundary, and wake the agent
on the other side with a note carrying its message.

1. **Which restarts wait.** A native agent that stays native keeps today's path unchanged (its own
   row, an immediate end of its execution). Every other restart (the agent runs on an external
   harness, or the overlay names another harness) goes to the gateway as the agent's own
   (`at_turn_end`, `source` must be `agent:<id>` of the agent it targets) and is recorded as a
   pending `restart` command with source `self`.
2. **Validation is the gateway's, at the call.** The request passes `_prepare_restart_validation_blocking`,
   the harness probe and the model validation of any restart; only the status rule differs (running
   or idling instead of idling or terminated). A refusal reaches the caller before anything is
   recorded; the SDK turns the status line into the response's reason.
3. **The command is the barrier, not an interrupt.** It carries `restart_transition`
   (`target_harness`, `config_overlay`). A running turn is never cut short by it (the interrupt
   watcher excludes `source='self'`, as for a native self restart), the steering of messages that
   arrive meanwhile stops at it (they stay pending), and the claim that runs when the turn returns
   takes it first and alone. Messages behind it keep their order for the harness the agent lands on.
4. **The host applies it in one transaction.** `apply_hosted_lifecycle` stores the overlay, the new
   harness (with the same `harness_configured_at` and model-selection marks the operator transition
   writes), releases the incarnation, closes the command and queues the note, all in the commit that
   already ends a native restart. The sequence of `_execute_harness_transition` (switch note, session
   record, force terminate, row update, resurrect) is not repeated: its terminate and resurrect only
   exist to move a row between two states the apply already moves. What does not live in the database
   stays outside the commit and can run twice: the old harness's session record is removed before it,
   and the switch note in the outgoing history is written after it.
5. **What the caller sees.** The native harness ends its execution as before. An external harness
   cannot be stopped from the SDK (its CLI owns the turn), so the call returns a receipt, "applied
   after the current turn ends, not now ... end the turn", and the turn goes on until it ends on its
   own. An agent that keeps working is not interrupted; the restart waits. A second request while one
   waits is refused and names the first, so a call whose receipt was not read cannot queue two
   switches.
6. **Same harness keeps the session.** A restart that stays on an external harness keeps the session
   record (the host's own graceful-exit restart does the same), so the conversation resumes; only a
   change of harness removes it. The operator route still starts a fresh session on a same-harness
   restart; that is a difference this record accepts.

## Alternatives rejected

- **Run the existing transition from the agent's call.** It terminates the very process that calls
  it. Unsafe by construction.
- **Let the operator route accept a running agent and queue it.** It would also fix `meiva agents
  restart` and the peer SDK, but widens a route whose 409 other callers depend on, and gives a UI or
  a peer the power to queue a harness change under a running turn. The `at_turn_end` flag is gated to
  the agent's own source; widening it is a separate decision.
- **Write the row from the SDK, as the native path does.** The probe of the target harness and the
  explicit model rule live in the gateway; a second copy in the SDK would drift, and a harness
  process may not hold the machine's probe cache.
- **Apply the overlay and harness when the request is recorded.** The running turn reads its
  settings; a half-switched row mid-turn is a state nothing else produces.
- **Add `meiva.self.switch_harness`.** It would be an alias of `restart` with one overlay key.

## Consequences

- The host's apply now writes `agents_meta.harness`; a malformed `restart_transition` raises in the
  apply and leaves the command claimed (a half-applied switch is worse than a refused one).
- A request that waits while its agent crashes is superseded by the resurrection, as any restart
  queued in a previous life; the agent is not told. The command's payload records the outcome.
- The switch note follows the messages that were queued behind the request (it is written at apply
  time), so the first turn on the new harness reads those messages before the note.
- A target harness that fails to launch after the commit is the existing failure of a launch on a
  harness the probe called launchable a moment ago; the agent is on the new harness and the
  operator route (`idling`/`terminated`) switches it back.
