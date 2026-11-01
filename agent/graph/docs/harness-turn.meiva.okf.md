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

`agent/graph/claim/harness_turn.py`, `base/lm/harness/bridge.py` is how an agent born on `claude_code` or `codex` runs a
turn. The claim node keeps owning the durable inbound queue, the lifecycle
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

`before_llm`, `llm`, `before_exec`, `exec` and `after_exec` never run on such an
agent. That is not a convention: the harness turn is the whole loop — model call,
tool calls, transcript — and those nodes exist to drive the pieces of a loop the
external CLI already owns. `tests/test_harness_routing.py` runs a compiled graph
and asserts the set of nodes that executed.

The choice of a whole-turn node and the rejected single-LLM-call alternative are
recorded in [the harness decision](../../../docs/decisions/agents/graph/2026-10-19-external-harness.md#the-decision).

## Storage contract and external timeline (Plan B)

For an external harness (`harness != "meiva"`), the external CLI's on-disk session
file (`FileSessionRegistry`) is the **Execution Source of Truth**: the CLI owns its
context window, compaction, and continuation.

Under Plan B:
- External harnesses write UI timeline items strictly to the dedicated Postgres table
  `agent_harness_messages` (with monotonic `seq` per agent) and **never** read or write
  LangGraph `state.messages`.
- LangGraph checkpoints remain 100% the execution and UI store for native `meiva` agents,
  eliminating cross-harness schema corruption and serialization bugs.
- `harness_cursor` has been completely retired from `BaseAgentState`.
- `claim` drains the inbound queue, maps incoming messages to `TimelineItem`s, writes
  them to `agent_harness_messages`, and passes the pending text to `harness_turn` via
  `state.harness_pending_input`.
- `harness_turn` executes the bridge turn, streams `BridgeEvent`s, atomically appends
  committed timeline items to `agent_harness_messages`, and clears `harness_pending_input`.

## The bridge

`bridge.py` holds one adapter session per runtime (`AgentRuntime.harness_bridge`),
closing on eviction, `drop_agent` or host shutdown. Resumed sessions reuse the id
in `FileSessionRegistry`.

- `bridge_for` builds the handoff and `ScopedSessionRegistry` (which binds every
  load/save to `SessionRecord.harness`). The handoff is co-delivered once on the
  first turn of a fresh session.
- `run_turn` starts the session, submits turn text under the newest inbound row id
  (or `window-{cursor + len(pending)}` when the batch carries only notes such as a
  heartbeat check-in), and streams `BridgeEvent`s via `on_event`. Completed keys within
  the same session replay the ticket outcome without emitting a second completion event.
- When a turn is interrupted by a system/terminate signal (`InterruptReason.SYSTEM`), any tool
  calls and reasoning already projected during the turn are committed to `agent_harness_messages`
  before `claim` settles the termination inbound.
- A session that faults or closes mid-turn retires itself and raises a fatal stream
  abort; the next inbound opens a fresh resumed session from the registry. Only a
  failure that names this turn ends it, and a session failure ends it as a closed
  session rather than as this turn's verdict
  ([[base/lm/harness/docs/attribution.meiva.okf.md|attribution]]).
- Work a harness reports after its turn (a background command) becomes a
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

## Cost and usage

Each non-replayed completed turn logs the same `llm_usage` row the built-in loop logs, with
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

Because an external harness CLI owns its context window and turn loop, native
context/lifecycle mutations are native (`Harness.MEIVA`) only:

- **`fork`, `compact`, and `meiva.self.attach` are native-only**:
  - `fork` (`fork_from`) and `POST /api/agents/{id}/compact` reject external harnesses with HTTP 400 (`ForkHarnessMismatch` on fork; guarded in `claim` dispatch); the web UI hides `Fork` and `Compact` (and omits `/compact` from slash autocomplete) for external harnesses.
  - `POST /api/agents/{id}/restart` and the Web UI `Restart` action are supported on `idling` external-harness agents and for bidirectional harness switching on `idling`/`terminated` agents (requiring a non-empty `message`). Calling `meiva.self.restart()` from code remains native-only (raises `RuntimeError` on external harnesses).
  - Calling `meiva.self.compact()` or `meiva.self.attach()` on an external harness raises `RuntimeError`.
- **Supported `meiva.self` surface on external harnesses**:
  - `harness_sdk_expand_section` and `meiva.help(meiva.self)` expose only `meiva.self.terminate()`, `meiva.self.pause_heartbeat(duration)`, and read-only identity constants (`AGENT_ID`, `MACHINE_SPEC`, `MachineSpec`). Use `terminate` and `resurrect`, or `restart` via API/UI to stop and resume an external harness agent.

A cancelled turn keeps its message in the window, so the next turn re-submits it. `LAUNCHABLE_HARNESSES` gates a wake before any of this runs.
