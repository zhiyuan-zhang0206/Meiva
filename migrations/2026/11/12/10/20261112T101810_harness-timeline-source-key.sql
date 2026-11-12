-- Expand step for projecting an external agent's timeline from the CLI's own transcript
-- (docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md). A timeline row gets the
-- stable key of the transcript entry it was projected from, unique per agent, so a row read twice is written
-- once, with the version of the projection rules that wrote it; each transcript record gets a stored read
-- position and the whitelisted rows the projection needs (appended once, never updated, expired by age),
-- all written in one transaction. Nothing reads or writes any of it yet. Idempotent, so a fresh database that already has them replays it.
ALTER TABLE agent_harness_messages ADD COLUMN IF NOT EXISTS source_key TEXT;
ALTER TABLE agent_harness_messages ADD COLUMN IF NOT EXISTS projection_version INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_harness_messages_source_key
    ON agent_harness_messages (agent_id, source_key) WHERE source_key IS NOT NULL;

COMMENT ON COLUMN agent_harness_messages.source_key IS
    'Stable key of the transcript entry this row was projected from; NULL for rows the graph writes itself. Unique per agent.';

COMMENT ON COLUMN agent_harness_messages.projection_version IS
    'Version of the projection rules that produced this row; NULL for rows written before it existed.';

CREATE TABLE IF NOT EXISTS agent_harness_transcript_rows (
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    record TEXT NOT NULL,
    ordinal BIGINT NOT NULL,
    row JSONB NOT NULL,
    written_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (agent_id, session_id, record, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_agent_harness_transcript_rows_written_at
    ON agent_harness_transcript_rows (written_at);

COMMENT ON TABLE agent_harness_transcript_rows IS
    'The whitelisted rows of an external harness session''s transcript, filtered but otherwise as the CLI wrote them: appended once, never updated, expired by written_at. The timeline is re-projected from them.';
COMMENT ON COLUMN agent_harness_transcript_rows.ordinal IS
    'The row''s own position in its record; sparse, because rows off the whitelist are not stored.';

CREATE TABLE IF NOT EXISTS agent_harness_transcript_positions (
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    record TEXT NOT NULL,
    position TEXT NOT NULL,
    rebuild_ordinal BIGINT NOT NULL DEFAULT -1,
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
COMMENT ON COLUMN agent_harness_transcript_positions.rebuild_ordinal IS
    'Ordinal of the last stored row at a quiet point (no call open, no sub-agent record unlinked): a restart rebuilds the projector from the stored rows after it, so the work is bounded by one turn and not the session. -1 = from the start.';
COMMENT ON COLUMN agent_harness_transcript_positions.cli_version IS
    'Version of the CLI that wrote the last row read, for telemetry.';

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON agent_harness_transcript_positions TO meiva_runner;
        GRANT SELECT, INSERT ON agent_harness_transcript_rows TO meiva_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_gateway') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON agent_harness_transcript_positions TO meiva_gateway;
        GRANT SELECT, INSERT, DELETE ON agent_harness_transcript_rows TO meiva_gateway;
    END IF;
END $$;
