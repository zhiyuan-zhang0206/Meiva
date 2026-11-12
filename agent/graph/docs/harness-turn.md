# Harness turn

`agent/graph/claim/harness_turn.py`, `base/lm/harness/bridge.py` run an external harness for Meiva. The claim node keeps owning the durable inbound queue, the lifecycle
commands and the turn boundary; both native and external harnesses route from `claim` to `before_turn`, which then routes to `llm` (native) or `harness_turn` (external):

```
claim → before_turn ──(native)───→ llm ↔ exec ──→ claim
          └───────────(harness)──→ harness_turn ─→ claim
```

The rewrite from `llm` to `harness_turn` lives in `route_target`, while `before_turn` is shared by both runtimes. On an external harness the claim takes only control rows (and leaves chat, system notes and heartbeats in the inbox); when it finds none but the head of the inbox holds deliverable rows, it routes to `before_turn` with that head as a read-only view in `harness_view`. Pre-turn hooks (`SkillRecallHook`, `newly_installed_skills`, and plugin `before_turn` hooks) inspect those messages and turn any note they emit into a synthetic input in `harness_inputs` (`hook-notes-<row id>`; delivery puts its text after the wake's rows in the one input they are handed over as, so the harness's first request already holds the message; a wake with no row text merges the note and its other synthetic inputs into one input the same way); `state.messages` stays empty (`[]`) in the checkpoint. Control kinds that hand text to the harness (restart or resurrect notes, a fork marker) and a context reset's continuation are synthetic inputs too, keyed `control-<newest row id>` and `context-reset-...`. A compaction parks its destination in `ContextReset.resume` (`before_turn`) and `init_context` routes there so the post-reset turn also runs `before_turn` before entering `harness_turn`.

## Turn boundaries

A **big turn** is one continuous stretch of foreground work from a wake until
the executor hands control back or reaches a terminal outcome. The native graph
executes the individual nodes. An external harness owns its loop, so the graph
waits in one node for the same boundary. Meiva provides no configurable limit
on the duration or model-call count of an external big turn. Operations such
as process startup, transport writes, and shutdown retain their own bounds.

A **small turn** is one model call, its input, and the work it requests. Native
Meiva owns those boundaries. External adapters may report a call or a per-call
usage receipt, depending on the protocol. Text, reasoning, and tool events
provide independently visible activity even when a model-call boundary is
unobservable.

A background command has its own lifecycle. If foreground work continues or
waits for it, the big turn remains active. When foreground work hands control
back, a later background completion can wake the agent through a new inbound
and start another big turn.

See [foreground input and cancellation](harness-foreground.md) for the execution contract.

## External timeline

The provider session owns execution state and continuation. Inbound timeline entries are
written when the harness reports the input in the model's context (ACK 2), in the transaction that completes its
inbox row (see [foreground input and cancellation](harness-foreground.md)). Native Meiva keeps its timeline in LangGraph
checkpoints. The external activity projector writes to
`agent_harness_messages` as events arrive.

A CLI sub-agent's text (`SubagentText`) is written as a capped, non-user-facing `subagent_text` item beside the
call that started it, and the tool calls it made are written as ordinary tool items tied to the same call
([sub-agent text](../../../base/lm/harness/docs/subagent-text.md)).

The first text or reasoning fragment, complete blocks, and tool start and
result checkpoints are written while the big turn is active. Additional
fragments are coalesced within a short checkpoint interval. Each item has a
stable flat `<seq>.0` ID and a monotonic revision. A tool result revises its
pending output in place, even if other tools or text appear in the meantime.
The hosted runtime incarnation fences writes. After each commit the same
checkpoint is published to the live timeline, and reloads read it from storage.
Incomplete text remains visible in the work history; only completed chat
blocks count as replies.

Cancellation and failure preserve committed work. Terminal handling flushes
pending text (a reply cut off mid-stream is sealed as it stood, so it is
delivered and recorded like a completed one), marks tools whose result is unavailable, and records a failure
reason when the harness faults. A host custody tick keeps the invocation
owned while the provider is quiet. It produces no timeline activity.

## Transcript-sourced timeline (setting on; default off)

While an external Claude Code agent's `harness_transcript_timeline` setting is on, the timeline is not built from
the adapter's events. The agent host builds a session-scoped writer with the runtime
([`agent/harness_timeline`](../../harness_timeline/README.md)); it reads the CLI's own transcript by stored position,
keeps the whitelisted rows in the database, and in one transaction per read appends keyed timeline items, settles
the inbound rows the entries prove delivered and moves the position. What changes in this node when the context
carries the writer (`ctx.harness_timeline`), and nothing else does:

- The adapter events no longer write content: text, reasoning, tool and sub-agent events are not folded into items
  (the adapter keeps mapping them, for the live overlay of the plan), and an input's echo (`InputInContext`) no
  longer writes its entries or settles its row; the writer does both from the record, so the timeline and the inbox
  never disagree.
- The invocation registers what it hands over (`register_input`, before `deliver()`), delivers the inbox as before,
  and ends a turn by waiting in the writer until the record shows the turn's closing entry, or stays quiet for 1 s,
  or 5 s have passed; a missing closing entry is the event `harness_timeline_turn_end_unseen`, never a failure. A
  turn whose result reports an error ends with that error.
- Usage is logged by the writer's effects, once per model message (`message.id`), not from the turn's result frame:
  the main agent's as `usage_kind="agent"` and a sub-agent's as `usage_kind="agent_subagent"` with `parent_call_id`
  (when the link is known), priced on its own model. The sub-agents' tokens are therefore counted; the figure rises by
  exactly that spend. A message the CLI cut short (an interrupt) has only a placeholder count in the record and is
  not priced: that is a known gap in the accounting, counted by the event `harness_timeline_usage_unreliable` (one per
  message, with the message id and turn); a model without a price is warned about once per runtime and its rows are written unpriced.
- The turns the invocation finishes are paired with the record's closings by identity, not by count: the stream says
  which of our inputs were in the turn (`InputInContext`), the writer knows which record turn each was delivered in
  (the row that settled it) and waits for those turns' own closings, and the spend compared with the result's is that
  of those turns. A closing that shows up late, a result frame that never came and the turns the CLI ran on its own
  (no input of ours in them, nothing to wait for) therefore shift nothing for the turns after them. A turn that ended
  in error is waited for like any other (its failure row closes it) and its spend is not compared with the result's.
- A stop's passive note still rides into the CLI with the next input but writes no timeline entry of its own; the
  record's own interrupt row is the only "interrupted" marker, and the early write of #8911 does not apply.
- Failure and restart: see [foreground input and cancellation](harness-foreground.md#transcript-sourced-agents).
- The CLI finds its login through `USER` and `LOGNAME` besides `HOME`; the session's child environment carries all
  three (`HOST_PASSTHROUGH_KEYS` in `base/host/env/agent_env.py`, part of `session_child_env_keys`), and the
  transcript the writer reads is under that `HOME` (`<home>/.claude/projects/<workspace slug>/<session id>.jsonl`).

With the setting off, none of this exists and the path above is unchanged. The setting and the event-sourced path
are deleted when all three CLIs have cut over.

## The bridge

`bridge.py` holds one adapter session per runtime (`AgentRuntime.harness_bridge`),
closing on eviction, `drop_agent` or host shutdown. Resumed sessions reuse the id
in `FileSessionRegistry`.

- `bridge_for` builds the handoff and gives each adapter the registry, which
  keeps one record per harness. The handoff is co-delivered once on the
  first turn of a fresh session; `HarnessBridge` tracks delivery per active session
  so later `set_handoff` calls on a warm or resumed session never re-prepend the
  handoff document. `_build_grounded_handoff` also saves the document to
  `meiva_instructions.md` in the agent's workspace and ends the message with a pointer
  to it (see [the handoff document](../../../base/lm/harness/docs/handoff.md)); after
  a compaction the bridge puts a short reminder in front of the next turn's input.
- `deliver(inputs)` ensures the session, puts the handoff (first input of a fresh session) or the
  compaction reminder (first input after the CLI compacted) in front of the first input, and hands the
  batch over; `subscribe()` returns the session's events from now on, so a caller subscribes first. The
  bridge never waits for a turn: the graph reads events and decides. Inbound text omits per-turn visibility
  injection; a reply that breaks the contract triggers a correction input inside the same invocation.
- A session that faults or closes mid-turn retires itself and raises a fatal stream
  abort; the next inbound opens a fresh resumed session from the registry. Only a
  failure that names this turn ends it, and a session failure ends it as a closed
  session rather than as this turn's verdict
  ([attribution](../../../base/lm/harness/docs/attribution.md)). Settled turn aborts
  clear `harness_inputs` alongside setting `halted=True`, so an aborted harness
  turn leaves no stale synthetic input in the checkpoint.
- A non-empty notice a harness reports after its turn becomes a
  `system:harness` inbound (`base/agents/inbox/harness_notice.py`), so
  the agent wakes on it through this same path.
- Retirement is asynchronous (`HarnessSessions.retire`), and the next turn awaits
  `join` before starting a replacement CLI. Cancelling a waiter does not cancel the
  close; close errors remain fenced across later joins, and observed process residue
  raises after the settle window. An unobserved process tree logs
  `harness_close_unobserved`.
- Missing SDKs or refused submissions raise `HarnessSetupError` and settle on the
  same fatal-stream path.
- A foreground interrupt calls `interrupt` via `subscribe_interrupt` and logs the
  adapter's `settled` report verbatim (`None` stays unobservable). It reads the
  terminal result on the same session so background work remains owned there.

Configuration updates and service shutdown use the same durable restart path as
Meiva turns: an interrupted harness turn keeps the restart cause, then claim and
ownership release settle it before continuation. An idle agent gains no automatic
continuation. The shared deadlines and shutdown fallback are owned by
[Agent-host graceful exit](../../../services/agent_runner/agent_host/lifecycle/docs/graceful-exit.md).

An explicit external harness restart or a harness switch delivers its restart
message as a lifecycle system note. The note starts execution on the selected
harness and appears as a system marker in the timeline. External harnesses
share one display store, so an external-to-external switch writes this marker
once. A switch across the native checkpoint and external display stores also
records a handoff in the outgoing history.

## Cost and usage

This section describes the adapter-event path (setting off); a transcript-sourced agent logs usage per model
message from the record, see above.

Each non-replayed terminal result with observed usage logs the same `llm_usage` row the built-in loop logs, with
`usage_kind="agent"` and `source` set to the harness name, so the two runtimes
stay comparable in one place. A turn whose provider reported no tokens logs
nothing.

Accounting never decides whether a reply survives: the turn has finished by the
time its row is written, so usage a price book refuses — a provider reporting
cache reads outside the prompt, a model missing from the catalog — logs
`harness_usage_log_failed` and leaves the reply in the window. The gap belongs in
the log, not in answers the user already paid for.

Execution model/effort resolve through the registry binding described in
[Harness execution models](../../../base/agents/docs/harness/harness-models.md).

## Capability boundary

Native-only context/lifecycle operations (`fork`, `compact`, `meiva.self.attach`)
versus supported external-harness `meiva.self` (including `restart`, applied at the turn
boundary) and API restart surfaces are defined in
[Harness turn capability boundary](harness-capability-boundary.md).
