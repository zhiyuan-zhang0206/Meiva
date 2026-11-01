-- Schema reset anchor: the preceding 8 deltas are folded into db/schema.sql.
-- The runner checks the complete frozen generation before stamping this name.

ALTER TABLE agents_meta SET (
    fillfactor = 85,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_vacuum_cost_limit = 1000,
    autovacuum_vacuum_cost_delay = 2
);

ALTER TABLE inbound_messages SET (
    fillfactor = 85,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_vacuum_cost_limit = 1000,
    autovacuum_vacuum_cost_delay = 2
);

CREATE INDEX IF NOT EXISTS idx_inbound_pending_created_at
    ON inbound_messages (created_at) WHERE status = 'pending';

ALTER TABLE agent_tasks
    DROP COLUMN IF EXISTS token_budget,
    DROP COLUMN IF EXISTS usd_budget,
    DROP COLUMN IF EXISTS token_used,
    DROP COLUMN IF EXISTS usd_used,
    DROP COLUMN IF EXISTS token_budget_notified_at,
    DROP COLUMN IF EXISTS usd_budget_notified_at;
