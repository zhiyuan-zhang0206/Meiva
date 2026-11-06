---
type: doc
title: Harness turn (external turn engines)
description: "`agent/graph/claim/harness_turn.py`, `base/lm/harness/bridge.py` — the graph's one node for an agent whose turn runs on an external CLI, the session bridge behind it, and the submission cursor that keeps the harness transcript and the graph window in step."
tags:
- agent
- graph
- external-agents
---

# Harness turn

`agent/graph/claim/harness_turn.py`, `base/lm/harness/bridge.py` run an external harness for Meiva. The claim node keeps owning the durable inbound queue, the lifecycle
commands and the turn boundary; when the batch would hand work to the built-in
loop, it routes to `harness_turn` instead:

```
claim ──(native)──→ before_llm → llm → before_exec → exec → after_exec
  └──(harness)────→ harness_turn ──→ claim
```

The rewrite lives in `route_target`, and it covers one more producer than claim's
own verdicts: a compaction parks its destination in `ContextReset.resume` and
`init_context` jumps there, so that jump goes through `route_target` too. Without it
a compacted external-harness agent resumes into `before_llm`, where the native model
answers while the CLI session sits idle.

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

See [[agent/graph/docs/harness-foreground.meiva.okf.md|foreground input and cancellation]] for the execution contract.

## External timeline

The provider session owns execution state and continuation. The Meiva claim
node stages inbound timeline entries and commits them on provider acceptance. Native Meiva keeps its timeline in LangGraph
checkpoints. The external activity projector writes to
`agent_harness_messages` as events arrive.

The first text or reasoning fragment, complete blocks, and tool start and
result checkpoints are written while the big turn is active. Additional
fragments are coalesced within a short checkpoint interval. Each item has a
stable flat `<seq>.0` ID and a monotonic revision. A tool result revises its
pending output in place, even if other tools or text appear in the meantime.
The hosted runtime incarnation fences writes. After each commit the same
checkpoint is published to the live timeline, and reloads read it from storage.
The IM bridge accepts completed chat blocks. Incomplete text remains visible
in the work history and is excluded from outbound message delivery.

Cancellation and failure preserve committed work. Terminal handling flushes
pending text, marks tools whose result is unavailable, and records a failure
reason when the harness faults. A host custody tick keeps the invocation
owned while the provider is quiet. It produces no timeline activity.

## The bridge

`bridge.py` holds one adapter session per runtime (`AgentRuntime.harness_bridge`),
closing on eviction, `drop_agent` or host shutdown. Resumed sessions reuse the id
in `FileSessionRegistry`.

- `bridge_for` builds the handoff and `ScopedSessionRegistry` (which binds every
  load/save to `SessionRecord.harness`). The handoff is co-delivered once on the
  first turn of a fresh session; `HarnessBridge` tracks delivery per active session
  so later `set_handoff` calls on a warm or resumed session never re-prepend the
  handoff document.
- `run_turn` starts the session, submits turn text under the newest inbound row id
  (or `window-{cursor + len(pending)}` when the batch carries only notes such as a
  heartbeat check-in), and streams `BridgeEvent`s via `on_event`. Completed keys within
  the same session replay the ticket outcome without emitting a second completion event.
- A session that faults or closes mid-turn retires itself and raises a fatal stream
  abort; the next inbound opens a fresh resumed session from the registry. Only a
  failure that names this turn ends it, and a session failure ends it as a closed
  session rather than as this turn's verdict
  ([[base/lm/harness/docs/attribution.meiva.okf.md|attribution]]).
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
- `interrupt` races via `subscribe_interrupt`, logs the adapter's `settled` report
  verbatim (`None` stays unobservable), and closes/retires the session in `finally`.

Configuration updates and service shutdown use the same durable restart path as
Meiva turns: an interrupted harness turn keeps the restart cause, then claim and
ownership release settle it before continuation. An idle agent gains no automatic
continuation. The shared deadlines and shutdown fallback are owned by
[[services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md]].

An explicit external harness restart or a harness switch delivers its restart
message as a lifecycle system note. The note starts execution on the selected
harness and appears as a system marker in the timeline.

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
[[base/agents/docs/harness/harness-models.meiva.okf.md|Harness execution models]].

## Capability boundary

Native-only context/lifecycle operations (`fork`, `compact`, `meiva.self.attach`,
`meiva.self.restart`) versus supported external-harness `meiva.self` and API restart
surfaces are defined in
[[agent/graph/docs/harness-capability-boundary.meiva.okf.md|Harness turn capability boundary]].
