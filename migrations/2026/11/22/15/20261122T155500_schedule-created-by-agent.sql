ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS created_by_agent_id BIGINT REFERENCES agents(id) ON DELETE SET NULL;

COMMENT ON COLUMN schedules.created_by_agent_id IS
    'Agent that created this schedule (provenance only). Does not affect execution identity, workspace, machine placement, or on_owner_terminate lifecycle cancellation.';

CREATE INDEX IF NOT EXISTS idx_schedules_created_by_agent_id
    ON schedules (created_by_agent_id)
    WHERE created_by_agent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_schedules_owner_agent_id
    ON schedules (owner_agent_id)
    WHERE owner_agent_id IS NOT NULL;
