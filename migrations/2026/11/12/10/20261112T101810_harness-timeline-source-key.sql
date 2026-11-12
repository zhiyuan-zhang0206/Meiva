-- Expand step for projecting an external agent's timeline from the CLI's own transcript
-- (docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md). A timeline row gets the
-- stable key of the transcript entry it was projected from, unique per agent, so a row read twice is written
-- once; and each transcript record gets a stored read position, written in the transaction that writes its
-- rows. Nothing reads or writes either yet. Idempotent, so a fresh database that already has them replays it.
ALTER TABLE agent_harness_messages ADD COLUMN IF NOT EXISTS source_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_harness_messages_source_key
    ON agent_harness_messages (agent_id, source_key) WHERE source_key IS NOT NULL;

COMMENT ON COLUMN agent_harness_messages.source_key IS
    'Stable key of the transcript entry this row was projected from; NULL for rows the graph writes itself. Unique per agent.';

CREATE TABLE IF NOT EXISTS agent_harness_transcript_positions (
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    record TEXT NOT NULL,
    position TEXT NOT NULL,
    cli_version TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (agent_id, session_id, record)
);

COMMENT ON TABLE agent_harness_transcript_positions IS
    'How far each transcript record of an external harness session has been read into agent_harness_messages.';
COMMENT ON COLUMN agent_harness_transcript_positions.record IS
    'The record read: main, or a sub-agent record id.';
COMMENT ON COLUMN agent_harness_transcript_positions.position IS
    'The adapter''s own opaque mark of how far the record has been read; the writer stores it and never reads it.';
COMMENT ON COLUMN agent_harness_transcript_positions.cli_version IS
    'Version of the CLI that wrote the last row read, for telemetry.';

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON agent_harness_transcript_positions TO meiva_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_gateway') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON agent_harness_transcript_positions TO meiva_gateway;
    END IF;
END $$;
