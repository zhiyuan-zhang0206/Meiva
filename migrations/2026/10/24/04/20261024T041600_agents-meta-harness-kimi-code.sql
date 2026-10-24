-- Add 'kimi_code' to the allowed agents_meta.harness CHECK constraint so agents
-- born on the Kimi Code external harness can be persisted.
--
-- Idempotent DROP-then-ADD matches db/schema.sql and 20261019T092549_agents-meta-harness.sql.

ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_harness_check;
ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_harness_check
    CHECK (harness IN ('meiva', 'claude_code', 'codex', 'antigravity', 'kimi_code'));

COMMENT ON COLUMN agents_meta.harness IS
    'The agent loop runtime this agent was born on (meiva / claude_code / codex / '
    'antigravity / kimi_code). Immutable: a trigger rejects any UPDATE that changes '
    'it, and a fork inherits its source''s value.';
