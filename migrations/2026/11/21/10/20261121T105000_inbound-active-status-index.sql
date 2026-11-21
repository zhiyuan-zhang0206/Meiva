-- Cover active (pending + claimed) per-agent inbox scans used by
-- `_claim_lifecycle_command`, `peek_head_batch`, `claimed_deliverable`,
-- `reserve_head_batch`, and `finalize_claimed_inbounds`. Without this partial
-- index, `status IN ('pending', 'claimed')` and `status = 'claimed'` fall back
-- to scanning every historical `done` row for the agent (37k-42k rows / ~157 MiB
-- per scan on long-lived agents).
--
-- The new index on `(agent_id, created_at, id) WHERE status IN ('pending', 'claimed')`
-- strictly subsumes `idx_inbound_per_agent_pending` (`(agent_id, created_at) WHERE
-- status = 'pending'`), so the old pending-only index is dropped in the same migration
-- to avoid double index maintenance on every inbound state transition.
--
-- Cost and locking: the migration runner wraps each migration in a single transaction,
-- so `CONCURRENTLY` is not used. Plain `CREATE INDEX` takes a SHARE lock (blocking
-- writes while building) and scans `inbound_messages` once (~425k rows, indexing only
-- the active pending/claimed subset); `DROP INDEX` takes ACCESS EXCLUSIVE briefly.
-- This runs only in the `meiva start` migration window while all services that read or
-- write `inbound_messages` are stopped, so nothing waits on it.
CREATE INDEX IF NOT EXISTS idx_inbound_per_agent_active
    ON inbound_messages (agent_id, created_at, id)
    WHERE status IN ('pending', 'claimed');

DROP INDEX IF EXISTS idx_inbound_per_agent_pending;
