-- Drop the harness immutability trigger and function so agents can switch harnesses on restart.
DROP TRIGGER IF EXISTS agents_meta_harness_immutable ON agents_meta;
DROP FUNCTION IF EXISTS reject_agents_meta_harness_update();

-- Create dedicated append-only UI display table for external harnesses (Plan B).
CREATE TABLE agent_harness_messages (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    seq INT NOT NULL CHECK (seq >= 0),
    harness TEXT NOT NULL,
    item JSONB NOT NULL,
    inbound_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (agent_id, seq)
);

CREATE INDEX idx_agent_harness_messages_agent_seq ON agent_harness_messages (agent_id, seq DESC);

COMMENT ON TABLE agent_harness_messages IS
    'Append-only timeline item store for agents running on external harnesses (claude_code, codex, antigravity, kimi_code).';
COMMENT ON COLUMN agent_harness_messages.agent_id IS
    'Owning agent id.';
COMMENT ON COLUMN agent_harness_messages.seq IS
    'Contiguous zero-based display sequence number per agent.';
COMMENT ON COLUMN agent_harness_messages.harness IS
    'The external harness that produced or owns this timeline item.';
COMMENT ON COLUMN agent_harness_messages.item IS
    'Rendered TimelineItem JSON payload for UI consumption.';
COMMENT ON COLUMN agent_harness_messages.inbound_id IS
    'Optional inbound message id if this item was produced from a specific inbound trigger.';
COMMENT ON COLUMN agent_harness_messages.created_at IS
    'Timestamp when the item was inserted.';

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT, INSERT ON agent_harness_messages TO meiva_runner;
        GRANT USAGE, SELECT ON SEQUENCE agent_harness_messages_id_seq TO meiva_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_gateway') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON agent_harness_messages TO meiva_gateway;
        GRANT USAGE, SELECT ON SEQUENCE agent_harness_messages_id_seq TO meiva_gateway;
    END IF;
END $$;
