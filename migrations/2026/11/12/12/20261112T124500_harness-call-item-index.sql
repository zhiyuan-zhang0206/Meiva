-- The timeline item of a CLI tool call is found by the CLI's own id (item->>'tool_use_id'): the transcript writer
-- asks for it when a sub-agent's output arrives whose spawning call is in an earlier batch or an earlier process.
-- Without an index every such lookup scans the agent's whole timeline. Partial on the kind, so only call items are
-- indexed. Idempotent, so a fresh database that already has it replays it.
--
-- Locking: the plain (not CONCURRENTLY) CREATE INDEX blocks writes to agent_harness_messages while it builds. That is
-- accepted: migrations are applied only by `meiva start`, and during an update the services are stopped at that point.
CREATE INDEX IF NOT EXISTS idx_agent_harness_messages_call
    ON agent_harness_messages (agent_id, (item->>'tool_use_id'))
    WHERE item->>'kind' = 'agent_code';
