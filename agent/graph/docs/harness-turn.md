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

## Record-read timeline (external harnesses)

An external agent's timeline is not written to a database table: it is read from the CLI's record when asked for
([the decision](../../../docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md); the runner's side is the
`harness_timeline_read` operation, the projection is in [reading a record](../../../base/lm/harness/docs/transcript-projection.md)).
`reads_timeline_from_record` (`base/api_contracts/agent_contract.py`) covers all external harnesses (`claude_code`, `codex`, `antigravity`):

- The text the adapter streams (`TextDelta`, `ReasoningDelta`) is shown by `LiveOverlay` (`harness_overlay.py`), which the sink
  feeds every event: it emits `chat_start` / `chat_delta` / `reasoning_start` / `reasoning_delta` under temporary ids
  `live.<turn id>.<block>` (the same coalescing and visibility fields as a built-in agent's stream) and keeps nothing
  that is stored, projected, delivered or counted. A snapshot that commits a block lists its live id in `retires`, found by
  the adapter's block id, else by the committed text starting with the streamed text; the end of a turn, an interruption or a
  fault (`finish`) reads the record once more and retires what is left, in a snapshot of its own. Pending deltas are sent
  before every retirement and a retired block's later deltas are dropped. Decision:
  [the streaming overlay](../../../docs/decisions/agents/graph/2026-11-25-external-harness-streaming-overlay.md).
- The live sink is `ReadTimelineSink` (`harness_read_sink.py`). The adapter's events are signals: any of them marks the
  sink dirty, and after half a second it reads the bytes the CLI appended since its last read, projects them with the
  cold read's function and publishes a `TimelineSnapshot` (ids are row offsets, so the page merges by id; the snapshot
  carries the item published before it and every call still running). A single consumer applies
  notifications in order and waits up to two seconds for a closing row. The foreground keeps
  settling events and steering while that consumer waits. The invocation drains the notification
  queue after choosing its outcome. The sink's state is in memory only. A reply the sink could
  not deliver is retried by the next invocation when it reads the last turn and delivers by key.
  When the starting record cannot be opened, the sink uses its initial file size as an upper
  bound on historical rows. If even the starting boundary is unknown, migration-era delivery
  waits for a later invocation to establish it. Existing keyed publications can still deduplicate.
- The foreground folds adapter events into `TurnProtocol` for abort handling, input handback,
  wake polarity and reply correction. It commits inbox settlements before notifying the live sink.
  A normal completion with activity or reply text must have a live-readable adapter record. The
  foreground checks for that file within the record's close window independently of projecting
  rows. A missing or unsupported record reports a setup failure naming the session and location.
  An interrupted or faulted turn keeps its original disposition while the sink flushes the view.
- An input is settled by the stream's acknowledgement (`InputInContext`) or by reading the record after a failure, and
  only the inbound row is set `done` (`commit_handed`, `commit_reconciled`): no database timeline rows are written for
  delivered inputs, a stop's passive note, an unconfirmed input, a lost turn, a restart, a harness switch, a fork or
  the handoff seed. A message appears on the page when the read of the record shows its row, and the page's pending
  strip clears on the item's `inbound_id` and on the `InboundCommitted` event the sink publishes with it.
- The reply to the user is delivered from the projected items, once each, under the harness record's stable key
  (`capture_keyed_items`). For a forked Claude Code agent whose cloned `.jsonl` carries the parent's history (with copied `user` rows stamped with the child's first `promptId`), `ReadTimelineSink` anchors and persists a per-session fork delivery floor (`<agent_id>.<harness>.floor`) at the start of the child's own rows so pre-fork parent replies stay readable on the timeline without ever publishing into `agent_publications`. The reply-visibility check of a finished reply, and with it the correction input, is made on the
  `AssistantText` event or the terminal reply text if no reply event arrived.
- Usage is logged from the turn's result frame (`log_turn_usage`); the sub-agents' tokens are in
  that figure only as the CLI reports them.
- The CLI finds its login through `USER` and `LOGNAME` besides `HOME`; the session's child environment carries all three
  (`HOST_PASSTHROUGH_KEYS` in `base/host/env/agent_env.py`), and the record is under that `HOME`
  (`<home>/.claude/projects/<workspace slug>/<session id>.jsonl` for Claude Code).

Codex and Antigravity use the same record-read path ([the plan](../../../future/agent/transcript-timeline-source.md)).

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
  close. Observed process residue raises after the settle window, and that fence is
  re-checked on every later join: it refuses while one of the residue pids is alive
  and lifts (`harness_close_residue_gone`) once none is. Any other close error stays
  fenced across later joins. Each refusal is a fresh `HarnessCloseFenceError` caused
  by the stored error. An unobserved process tree logs `harness_close_unobserved`.
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
message as a lifecycle system note on the selected harness. A harness switch writes nothing to
the outgoing history, whether that is the native checkpoint or an external CLI record: the switch is
shown by the agent's [harness epochs](../../../base/agents/epochs/docs/epochs.md), closed and
opened in the transaction that stores the new harness.

## Cost and usage

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
