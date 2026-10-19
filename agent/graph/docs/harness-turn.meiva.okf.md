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

## Why a node and not a smaller llm node

The ruled alternative was to swap the body of one LLM round. It cannot work:
`llm` is one model call whose contract is "return tool calls for `exec`", while a
harness turn performs any number of tool calls internally and returns one text.
Routing from claim also keeps the one implementation of terminate / cancel /
restart / resurrect / fork — several hundred lines of durable-intent handling —
instead of a second copy that would drift.

## The submission cursor

The harness owns the transcript of a resumed session; the graph owns
`state.messages`, which is the display mirror. `state.harness_cursor` is the join:
how many of the window the harness has already received.

- `init_context` sets it past the standing head it lays down on a cold start. The
  harness gets that context from its **handoff document**, not as messages, and
  the ruling forbids the bridge from touching the harness's system prompt.
- `harness_turn` submits everything after the cursor (minus the agent's own
  replies, which the cursor covers) and advances it over the messages it sent
  plus the reply it appended.

A crash therefore re-submits exactly what the harness never received, and a
resumed session never re-sends what its transcript already holds. The window can
grow past the cursor only while a turn is in flight; the cursor lags by one turn
at most.

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
- `run_turn` submits with the newest inbound row id as its idempotency key and
  waits for **that** turn's completion. A session opened with a handoff reports
  that turn first; it comes back as a `prelude` entry with its own text and usage,
  and the node commits it as its own message, so the waking acknowledgement never
  reads as the answer to the first chat.
- A session that faults or closes mid-turn retires itself: the bridge drops it, and
  the next inbound opens a fresh one from the registry, where the provider still
  holds the transcript. The node raises that as a fatal stream abort, so the graph
  settles the turn the way it settles any aborted model call rather than stopping
  with half a reply in the window.
- Retirement is asynchronous, and the next turn waits for it. An evicted runtime
  leaves the cache immediately, so without that wait a wake a second later would
  start a second CLI on the same resumed session id while the first one is still
  inside its terminate/kill window. `HarnessSessions.retire` therefore keeps the
  close per agent and `join` is what the next turn waits on; the close itself waits
  out the adapter's settle window and reports residue instead of assuming it.
- A turn that never got a session (a missing provider SDK, a refused submission) is
  a setup failure, not a crash: the bridge raises `HarnessSetupError` and the node
  settles it on the same fatal-stream path.
- `interrupt` is called from the node's cancel race: the durable cancel path is
  the existing `subscribe_interrupt`, and the adapter's `settled` report is logged
  as it came back — `None` (unobservable CLI tree) is not rounded up to "reaped".
  An interrupt that raises is logged, not raised: the durable cancel row is still
  pending, and a raise would turn the user's stop into a failed turn.

## Cost and usage

Each completed turn logs the same `llm_usage` row the built-in loop logs, with
`usage_kind="agent"` and `source` set to the harness name, so the two runtimes
stay comparable in one place. A turn whose provider reported no tokens logs
nothing.

## What this slice does not do

Streaming deltas to the timeline, tool-call timeline items and explicit `/compact`
requests are not wired yet; they are the next slices of the same node.

A cancelled turn keeps its message in the window and does not advance the cursor,
so the next turn re-submits it — the same shape as the built-in loop, which keeps
the human message and answers it on the next call. The adapter's submission key
turns the re-submission into a replay when that turn did complete before the
interrupt landed. `LAUNCHABLE_HARNESSES`
gates a wake before any of this runs, so a harness whose adapter has not landed
still refuses the turn.
