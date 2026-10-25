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

## CQRS storage contract and submission cursor

For an external harness (`harness != "meiva"`), the external CLI's on-disk session
file (`FileSessionRegistry`) is the **Execution Source of Truth**: the CLI owns its
context window, compaction, and continuation. Postgres `state.messages` is strictly
an **append-only UI Timeline Projection** (`state.harness_cursor` marks how much of
`state.messages` is covered by committed harness turns and is never reset to `0`).

- An empty window writes `[SystemMessage(content=handoff.render()), *notes, *reset.tail]`
  with `harness_cursor = 0` (`model-visible ⟺ logged`): `messages[0]` is the rendered
  **handoff document** at `0.0`, followed by standing context notes (`timezone`, `memory`,
  `agent_id`, `agent_memory`, `inherited_memory`, `preloaded_skills`, plugin notes).
- On the first turn, `pending_messages(state)` co-delivers the handoff `SystemMessage`,
  standing context notes, and first user message in one submission.
- `harness_turn` runs `harness_pre_turn_hooks` and `node_lifecycle(HARNESS_TURN, ...)`,
  while `HarnessTurnProjector` projects `BridgeEvent`s (`TextDelta`, `AssistantText`,
  `ToolUse`, `ToolResult`) into live SSE events and `state.messages`, advancing
  `harness_cursor` over all sent and appended messages using one turn-scoped UTC `Clock`.

The cursor records committed graph turns, not durable provider acknowledgements;
a crash or cancel before commit can re-submit pending input across sessions.

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
  calls and reasoning already projected during the turn are committed to `state.messages`
  and covered by `harness_cursor` before `claim` settles the termination inbound.
- A session that faults or closes mid-turn retires itself and raises a fatal stream
  abort; the next inbound opens a fresh resumed session from the registry.
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

## Capability boundary

Because an external harness CLI owns its context window and turn loop, native
context/lifecycle mutations are native (`Harness.MEIVA`) only:

- **`fork`, `compact`, `restart`, and `meiva.self.attach` are native-only**:
  - `fork` (`fork_from`), `POST /api/agents/{id}/compact`, and `POST /api/agents/{id}/restart`
    reject external harnesses with HTTP 400 (`ForkHarnessMismatch` on fork; guarded
    in `claim` dispatch); the web UI hides `Fork`, `Compact`, and `Restart` (and
    omits `/compact` from slash autocomplete) for external harnesses.
  - Calling `meiva.self.compact()`, `meiva.self.restart()`, or `meiva.self.attach()`
    on an external harness raises `RuntimeError`.
- **Supported `meiva.self` surface on external harnesses**:
  - `harness_sdk_expand_section` and `meiva.help(meiva.self)` expose only
    `meiva.self.terminate()`, `meiva.self.pause_heartbeat(duration)`, and read-only
    identity constants (`AGENT_ID`, `MACHINE_SPEC`, `MachineSpec`). Use `terminate`
    and `resurrect` to stop and resume an external harness agent.

A cancelled turn keeps its message in the window and does not advance the cursor,
so the next turn re-submits it. `LAUNCHABLE_HARNESSES` gates a wake before any of
this runs.
