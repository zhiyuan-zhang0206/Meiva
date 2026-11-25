CREATE TABLE IF NOT EXISTS agent_harness_epochs (
    agent_id     BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL CHECK (seq >= 0),
    harness      TEXT NOT NULL CHECK (harness IN ('meiva', 'claude_code', 'codex', 'antigravity', 'kimi_code')),
    model        TEXT,
    session_id   TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    ended_at     TIMESTAMPTZ,
    source_start BIGINT CHECK (source_start >= 0),
    source_end   BIGINT CHECK (source_end >= 0),
    switched_by  TEXT NOT NULL CHECK (switched_by IN ('birth', 'user', 'self', 'operator', 'backfill')),
    reason       TEXT,
    PRIMARY KEY (agent_id, seq),
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK (source_end IS NULL OR source_start IS NULL OR source_end >= source_start)
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_harness_epochs_current_idx
    ON agent_harness_epochs (agent_id) WHERE ended_at IS NULL;

COMMENT ON TABLE agent_harness_epochs IS
    'Which harness an agent ran on, and when: one row per stretch of time on one harness, numbered from 0 per agent. Facts only, never conversation content; a timeline reader joins these rows to the record each harness keeps. The newest row (ended_at IS NULL) is the current one. A harness switch closes it and opens the next in the same transaction as the agents_meta.harness update; a restart that keeps the harness opens nothing.';

COMMENT ON COLUMN agent_harness_epochs.seq IS
    '0-based, per agent, monotonic: the order the stretches happened in.';

COMMENT ON COLUMN agent_harness_epochs.harness IS
    'The harness this stretch ran on; the same value set as agents_meta.harness.';

COMMENT ON COLUMN agent_harness_epochs.model IS
    'The model the agent was configured with when the stretch began; NULL when unknown (backfilled rows).';

COMMENT ON COLUMN agent_harness_epochs.session_id IS
    'The external CLI session the stretch ran in, once known; NULL for the built-in meiva harness and for rows written before the session was bound.';

COMMENT ON COLUMN agent_harness_epochs.started_at IS
    'When the stretch began: the birth, or the switch that opened it.';

COMMENT ON COLUMN agent_harness_epochs.ended_at IS
    'When the stretch ended; NULL for the current stretch.';

COMMENT ON COLUMN agent_harness_epochs.source_start IS
    'External harnesses only: the byte offset in the CLI record file where this stretch''s rows begin. The same file can serve two stretches of one harness (A, then B, then A again), so the offsets tell their rows apart. NULL when unknown; readers then fall back to started_at and ended_at.';

COMMENT ON COLUMN agent_harness_epochs.source_end IS
    'External harnesses only: the byte offset where this stretch''s rows end; NULL while the stretch is current, or when unknown.';

COMMENT ON COLUMN agent_harness_epochs.switched_by IS
    'What opened the stretch: birth, user (an operator request from a person), self (the agent restarted itself), operator (any other caller), or backfill (reconstructed from earlier notes by a migration, best effort).';

COMMENT ON COLUMN agent_harness_epochs.reason IS
    'The restart message that asked for the switch, truncated for display; NULL when none was given.';

COMMENT ON COLUMN agents_meta.harness IS
    'The agent loop runtime this agent currently runs on (meiva / claude_code / codex / antigravity / kimi_code). Set at birth (a fork inherits its source''s value) and changed only by a harness switch, which opens a new row in agent_harness_epochs in the same transaction.';
