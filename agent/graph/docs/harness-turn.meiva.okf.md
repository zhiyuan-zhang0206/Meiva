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

## The submission cursor

The harness owns the transcript of a resumed session; the graph owns
`state.messages`, which is the display mirror. `state.harness_cursor` is the join:
how much of the display window is covered by committed harness turns.

- An empty external-harness window starts at zero and contains no Meiva system
  prompt or standing notes. The harness gets its identity from its **handoff
  document**; the bridge never overrides the provider's system prompt. A reset
  tail remains pending input. Existing windows keep their cursor, so old synthetic
  heads are not re-submitted; the UI hides those legacy system-prompt entries.
- `harness_turn` submits everything after the cursor (minus the agent's own
  replies, which the cursor covers) and advances it over the messages it sent
  plus the reply it appended.

The cursor records committed graph turns, not durable provider acknowledgements.
A crash or interruption before that commit can re-submit the pending input even
when the provider received it. Provider transcript resume does not provide
cross-session submission-key deduplication.

## The bridge

`bridge.py` holds one adapter session per runtime (`AgentRuntime.harness_bridge`),
so its life is the runtime's: idle-TTL or LRU eviction closes it, `drop_agent`
(terminate, restart, crash) closes it, and the host's shutdown closes what is
left. A resumed session is the same session id from `FileSessionRegistry`, which
is why an agent id must be stable across resurrect.

- `bridge_for` builds the handoff — identity, machine, workspace, and one
  acknowledgement request — and the scoped registry. The handoff is injected once,
  when the session is created; a resume does not repeat it.
- `ScopedSessionRegistry` binds every load and save to the bridge's harness, so an
  adapter cannot resume another provider's transcript even if it forgets to say
  which harness it is; `SessionRecord.harness` is what makes that check possible.
- `run_turn` first awaits the handoff, then submits user input with the newest
  inbound row id as its idempotency key. A failed or interrupted handoff never
  launches the user task. Its successful completion remains a `prelude` entry
  with separate text and usage. Completed keys within the same adapter session
  replay the ticket outcome directly: no second completion event is emitted.
- A session that faults or closes mid-turn retires itself: the bridge drops it, and
  the next inbound opens a fresh one from the registry, where the provider still
  holds the transcript. The node raises that as a fatal stream abort, so the graph
  settles the turn the way it settles any aborted model call rather than stopping
  with half a reply in the window.
- Retirement is asynchronous, and the next turn waits for it. An evicted runtime
  leaves the cache immediately, so without that wait a wake a second later would
  start a second CLI on the same resumed session id while the first one is still
  inside its terminate/kill window. `HarnessSessions.retire` therefore keeps the
  close per agent and `join` is what the next turn waits on. Cancelling that wait
  cannot cancel the close. Close errors remain fenced across later joins; observed
  process residue raises after the bounded settle window instead of allowing a
  replacement runtime. The first wake after an eviction can therefore take about 25s before it builds a
  runtime — the SDK's own bounded close plus the settle window — which is rare and
  bounded, not a stall. A close that observed no process tree at all reports
  `harness_close_unobserved` rather than success: the pid comes from a provider SDK's
  private structure, and "nothing seen" is not "nothing running".
- A turn that never got a session (a missing provider SDK, a refused submission) is
  a setup failure, not a crash: the bridge raises `HarnessSetupError` and the node
  settles it on the same fatal-stream path.
- `interrupt` is called from the node's cancel race: the durable cancel path is
  the existing `subscribe_interrupt`, and the adapter's `settled` report is logged
  as it came back — `None` (unobservable CLI tree) is not rounded up to "reaped".
  The bridge closes and retires the session in `finally`, including when the
  provider interrupt fails or `turn/start` has not returned a provider turn id.
  The node logs interruption errors and lets claim consume the durable command.
  A close failure remains recorded on the bridge and blocks subsequent sessions.

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

## What this slice does not do

Streaming deltas to the timeline, tool-call timeline items and explicit `/compact`
requests are not wired yet; they are the next slices of the same node.

A cancelled turn keeps its message in the window and does not advance the cursor,
so the next turn re-submits it — the same shape as the built-in loop, which keeps
the human message and answers it on the next call. Retiring the adapter discards
its in-memory submission-key cache, so a later session resumes provider history
but may execute the pending input again. `LAUNCHABLE_HARNESSES` gates a wake before
any of this runs, so a harness whose adapter has not landed still refuses the turn.
