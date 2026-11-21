-- Cover active (pending + claimed) per-agent inbox scans used by
-- `_claim_lifecycle_command`, `peek_head_batch`, `claimed_deliverable`,
-- `reserve_head_batch`, and `finalize_claimed_inbounds`. Without this partial
-- index, `status IN ('pending', 'claimed')` and `status = 'claimed'` fall back
-- to scanning every historical `done` row for the agent (37k-42k rows / ~157 MiB
-- per scan on long-lived agents).
CREATE INDEX IF NOT EXISTS idx_inbound_per_agent_active
    ON inbound_messages (agent_id, created_at, id)
    WHERE status IN ('pending', 'claimed');
