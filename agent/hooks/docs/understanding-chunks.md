# Understanding Chunk Producers — When Chunks Are Enqueued

## Responsibility

`agent/hooks/understanding_chunks.py` — when understanding chunks are enqueued: after an llm turn (`due_chunk_update`) and at compaction (`enqueue_closing_chunk`, called by `stamp_compact_boundary`); see [Chunk-triggered Understanding](../../../base/agents/history/hierarchy/docs/chunks.md). The hook system itself is [Agent Hooks](hooks.md)'s to state.

## Config as values

Understanding producers take the agent's config as values. `due_chunk_update` takes the
turn's `AgentSlices` (the enabled gate, the chunk ratio, the model and its overrides);
`enqueue_closing_chunk` and `await_snapshot` take `understanding_enabled: bool`, which
the compaction path reads once. The decisions are functions of values:
`agent/hooks/chunk_turn.py:plan_turn` / `plan_closing` return the chunk to enqueue and the state
that follows it (or the segment's baseline, or nothing), and `snapshot_wait.py` (a
`strict` module) says whether a checkpoint holds a state's last message
(`snapshot_verdict`) and when a message was read (`read_time`). `understanding_chunks.py`
is the shell: it reads the config, enqueues through `enqueue_chunk`, polls the checkpoint
table and sleeps on the `timers` it is given (the context's clock). Its tests run the shell against the throwaway Postgres.

## Entry Points

- `agent/hooks/understanding_chunks.py` — the producers and the database shell
- `agent/hooks/chunk_turn.py` — `plan_turn` / `plan_closing`, the pure chunk decisions
- `agent/hooks/snapshot_wait.py` — `snapshot_verdict` / `read_time`, the pure checkpoint-wait decisions

Parent: [Agent Hooks](hooks.md).
