-- Harness is an immutable property of an agent (user ruling, task #5407): the
-- shell that runs the agent loop — Meiva's own runtime, or an external CLI
-- (Claude Code, Codex, Antigravity). Model and provider stay separate concerns.
--
-- Every existing row was born on the built-in runtime, which is exactly what the
-- default records. New rows carry the harness their spawn named, and a fork
-- inherits its source's value (enforced at the birth boundary in
-- base/agents/birth/harness.py: a fork naming a different harness is refused,
-- not warned about).
--
-- The column is written once, at INSERT: the trigger below rejects any UPDATE
-- that would change it, so no restart, resurrect, rollout or manual repair can
-- silently move an agent onto another runtime. Fixing a wrong value means a new
-- agent, not an update.
--
-- Idempotent on purpose (ADD COLUMN IF NOT EXISTS / DROP-then-ADD constraint /
-- REPLACE function / DROP-then-CREATE trigger): the baseline in db/schema.sql
-- already carries the same objects, and a fresh database replays this file.

ALTER TABLE agents_meta
    ADD COLUMN IF NOT EXISTS harness TEXT NOT NULL DEFAULT 'meiva';

ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_harness_check;
ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_harness_check
    CHECK (harness IN ('meiva', 'claude_code', 'codex', 'antigravity'));

CREATE OR REPLACE FUNCTION reject_agents_meta_harness_update() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'agents_meta.harness is immutable once the agent is born (% -> %)',
        OLD.harness, NEW.harness;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agents_meta_harness_immutable ON agents_meta;
CREATE TRIGGER agents_meta_harness_immutable
    BEFORE UPDATE OF harness ON agents_meta
    FOR EACH ROW
    WHEN (OLD.harness IS DISTINCT FROM NEW.harness)
    EXECUTE FUNCTION reject_agents_meta_harness_update();

COMMENT ON COLUMN agents_meta.harness IS
    'The agent loop runtime this agent was born on (meiva / claude_code / codex / '
    'antigravity). Immutable: a trigger rejects any UPDATE that changes it, and a '
    'fork inherits its source''s value.';
