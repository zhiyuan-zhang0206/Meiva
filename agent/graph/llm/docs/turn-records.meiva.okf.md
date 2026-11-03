---
type: doc
title: LLM turn records and activity persistence
description: The turn_end records, per-turn persistence, and streaming item ids of the llm node.
tags: [agent, graph]
---

# LLM turn records and activity persistence

One `turn_end` record per attempt (`agent/graph/llm/node.py:llm_attempt`):
ok=True at INFO; ok=False through `logger.opt(exception=True)`, so the failed
attempt's traceback reaches the record. Both emit sites name the owning agent
explicitly — the process-wide log binding is the no-agent sentinel, and the
Inspector activity projection drops records without it.

The record of the attempt that stops the model loop is emitted one node later, by the
turn boundary (`agent/graph/claim/turn_end.py`): only claim knows whether the turn ended
there, so it holds the measured call and writes it once, with `ended_with` — the closed
vocabulary of `base/agents/observation/ended_with.py`:

- `no_input` — claim ended the turn at its idle boundary: nothing more arrived;
- `budget` — a budget rail ended it (truncated output, exhausted retry budget), the only
  value that also appears on an `ok=False` record;
- `done` / `waiting:<object>` / `deferred:time` — the agent's own declaration, carried by
  `meiva.state_update` (`ended_with` + `ended_with_reason`) and validated where it enters
  the state: `waiting:` requires its object, and `deferred:time` requires the one-sentence
  reason that makes a dependency-free delay attributable.

A value outside that set is an invalid record. A turn that continues past its halt, and a
turn ended by terminate or restart, carry no `ended_with`: the loop did not end those by
running out of input, and it does not guess.

A failed try publishes no Error: retries may still succeed, so Error is
published once by the runloop after the retry layer is exhausted, while
Cancelled stays with the node. Only a success whose command carries `messages`
closes the heartbeat circuit breaker — the cancel-shaped return never does (no
stream completed; the partial generation was discarded).

Every completed turn persists through `persist_last_active` (`agent/graph/turn_stamp.py`) — the native `llm`
node and the external-harness `harness_turn` node both call it, so a harness
agent's `get_last_message` answers with its reply instead of `None` (#5511):
`last_active_at`
always advances (the heartbeat daemon's idle clock — the ops lifecycle
deliberately never writes it), `last_turn_fatal_at` clears the corpse marker,
`permanent_reject_streak` and its reason reset (the recovery breaker's only
reset), and `last_message_text` is stored only when the turn produced text
(survives compaction; `get_last_message` reads it back). The turn-progress mark
lands even when the DB write fails.

Post-stream finalization (`_finalize_turn_observability`) stamps
`meiva_created_at` and str-keyed per-block timing (`meiva_reasoning_ms_by_block` /
`meiva_code_ms_by_block`; `base/agents/history/timeline.py` reads them back),
logs `llm_usage`, and emits the live `TokenUsage` SSE — accurate only once the
stream completed. Retry lives in the node, not a graph-level policy (the host's
single graph serves every agent), and the wait clips to the budget the settled
attempt recorded on the exception (`RETRY_REMAINING_ATTR`). Streaming item ids
use `msg_idx` (the message count at node entry) plus the block index,
reproducing the gateway timeline endpoint's id for the same committed
AIMessage, so the frontend merges by stable key instead of a timestamp
heuristic. An empty stream returns an empty message to `before_exec`
(historical behavior).
