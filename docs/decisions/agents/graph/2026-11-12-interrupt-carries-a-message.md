# Interrupting another agent's turn is `interrupt`, and it carries a message

## Context

`meiva.agents.cancel(agent_id)`, `meiva agents cancel` and the stop button stopped another agent's
running turn and said nothing else. The agent that was stopped learned nothing:

- A native turn was cancelled by the host (`Task.cancel()`) and left no record at all, or at most the
  generic `[interrupted: no result was recovered ...]` for a dangling tool call. Nobody was named.
- An external harness's cancel row was written with `source='user'` and an empty body whoever asked, so
  an agent's call was recorded as the user's (17 such rows in one week), and the stopped agent
  sometimes reasoned from "the user rejected my tool call".
- The agent's heartbeat kept running. When it fired five minutes later the agent had no record of what
  happened, who stopped it, or why.

"Cancel" was also the wrong word. The turn is interrupted, not abandoned: the agent lives, its
background work and shell sessions are untouched, and a new input starts a new turn. It is not
`terminate` (the agent ends) or `restart` (a new process, and it already requires a message).

## Decision

1. **The operation is `interrupt`, and the message is required.** `meiva.agents.interrupt(agent_id,
   message)`, `meiva agents interrupt <id> -m TEXT --source ...` and `POST /api/agents/{id}/interrupt`
   replace `cancel` everywhere; there is no alias and no old route. A blank message is refused at each
   boundary. Like `restart`, an agent cannot interrupt another without saying why. The one exception is
   the web stop button, which has no text: a `user` source may omit the message (see 4).
2. **Write first, interrupt after.** One transaction on the gateway locks the target's row and, if it is
   running, writes in id order: a `system_note` tagged `lifecycle_interrupt` ("Your running turn was
   interrupted by agent:405. Their message follows."), the message as an ordinary `chat` inbound from
   the real initiator, and, for an external harness only, the `interrupt` control row its foreground
   watcher reacts to. After the commit the turn is interrupted: a native turn through the home host's
   `/cancel-turn`, an external harness through its watcher. A failed or unreachable interruption leaves
   the message queued and delivered; an interrupt of an agent that has no running turn is a plain chat
   (no note, no control row) and answers `not_running`. The answer is `interrupted | not_running`.
3. **The next input is the reason.** The native agent and the external harness both claim the note and
   the message in id order, so the first thing either reads afterwards names who interrupted it and why,
   before a heartbeat could arrive (a heartbeat needs the agent to be idle and unwoken; the message's
   wake starts the turn instead). The note states the fact only; what to do next is the agent's call.
   The host now starts that turn at once: `_pump` restarts a successor only after a completed turn, so a
   wake that arrived during a cancelled turn used to wait for the next scan. `cancel_turn(rearm=True)`
   (set by an interrupt, not by a force terminate) closes the gap.
4. **The stop button is a person's interrupt with no text.** It sends the same request as `user` with no
   message, writes one passive note ("Your running turn was interrupted by user.") and, for an external
   harness, the control row. It is a system-generated explanation, not a message put in the user's
   mouth, and the agent stays idle until a real input. The note enters the agent's message list and the
   timeline at once, without waking it to a model turn (user ruling, 2026-11-12: the inserted message
   must be in the agent's messages list, so it knows it was interrupted, but it does not wake it). A
   native agent's claim takes it and idles again; an external harness's foreground writes its timeline
   entry when the interrupt is confirmed, while the CLI's context still receives the note with its next
   input (sending the CLI input is waking it). See
   [turn interrupts](../../../../agent/graph/docs/turn-interrupts.md).
5. **No durable native control row.** #6488 removed it on purpose and this does not bring it back: a
   native interrupt is the host's task cancel plus ordinary inbound rows.
6. **The kind is renamed too.** `inbound_messages.kind` `cancel` becomes `interrupt`, and the audit event
   `cancel` becomes `interrupt`, by a migration (the constraint is replaced and existing rows updated).
   The live view's `Cancelled` projection event, the exec host's in-band `cancel` frame and `Task.cancel()`
   are implementation words for cancelling a task and keep their names. `agent_control_receipts` (no
   writer, historical) is untouched.
7. **Who is recorded.** The source is the real initiator: `agent:<id>` from the SDK, the CLI's `--source`
   (default `user`), `user` for the button. The `interrupt` audit event carries the target.

## Compared with the neighbours

| | `interrupt` | `terminate` | `restart` |
|---|---|---|---|
| Stops | the running turn, now | nothing now; the agent exits after its turn (force interrupts) | after a 10 s grace, the running work |
| Afterwards | same process, idle, then the message starts a turn | `terminated`; any message resurrects | a new process under the same id |
| Message | required (stop button: none, a note only) | optional, kept for resurrection | required |
| Agent is told | note + the message | `Termination was accepted from ...` | `Restart was accepted from ...: message` |
| Background work / shells | untouched | kept unless asked | may not survive |

## Consequences

- `meiva.agents.cancel` no longer exists; an agent that calls it gets `AttributeError`. A gateway and the
  runners must be updated together: after the migration an old runner cannot write `cancel` rows.
- Dashboards counting `sdk_call fn=agents.cancel` move to `agents.interrupt`.
- The interrupt note and message are separate inbound rows, so `recovery_notes_first` keeps the note
  ahead of the message if a reconciliation returns both to the queue.
- A native turn that ended between the check and the cancel is told it was interrupted although no
  turn was stopped; the answer is then `not_running`. The wording says only that the turn was
  interrupted by the sender, and the message is still the next input.
