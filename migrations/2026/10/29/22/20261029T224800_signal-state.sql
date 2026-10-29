-- A condition that stays true is one row, not one event per check: `signal_state` holds the open
-- episode of each (source, subject, condition), so a detector can say "still happening" and a
-- reconciler can close it when it stops. `telemetry_events` stays the append-only evidence; this
-- table only answers "is it still happening".
--
-- IF NOT EXISTS throughout because `db/schema.sql` already declares the table and every index:
-- a fresh database builds from there and then replays the post-baseline files it has not
-- stamped (migrations/README.md).
CREATE TABLE IF NOT EXISTS signal_state (
    id              BIGSERIAL PRIMARY KEY,
    -- Who reports (machine, service or subsystem), what the report is about, and the stable
    -- name of the condition within that pair. Three columns rather than one joined key: the
    -- reconciler and the reports aggregate by source, and a joined string would have to be
    -- split again or scanned whole.
    source          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    condition       TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The newest report: a resolver judges staleness from this, never from `opened_at`.
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- How many reports this episode absorbed. The reports themselves live in telemetry_events.
    count           BIGINT NOT NULL DEFAULT 1,
    -- Event ids, pointed at without a foreign key: telemetry_events is the high-rate append-only
    -- table, and a foreign key would put its insert behind this one.
    first_event_id  BIGINT,
    last_event_id   BIGINT,
    -- NULL means open. Closing is what makes the next report a new episode instead of a touch.
    resolved_at     TIMESTAMPTZ,
    -- Free text rather than a CHECK list: every condition grows its own reasons over time, and a
    -- CHECK would turn each new one into a migration. The vocabulary the write path accepts is
    -- `base/events/signals/state.py:ResolveReason`.
    resolve_reason  TEXT,
    -- Why the closer believed the condition was gone: a log line, an event id, a sample.
    evidence_ref    TEXT,
    -- A human read it. Not a close: an acknowledged condition keeps standing until the condition
    -- itself goes away, so this is a column and not a resolve reason.
    acked_by        BIGINT,
    acked_at        TIMESTAMPTZ
);

COMMENT ON TABLE signal_state IS
    'One row per open episode of a condition, touched while it persists and closed when it stops. telemetry_events stays the evidence; this table answers whether the condition is still true.';

COMMENT ON COLUMN signal_state.condition IS
    'Stable name of the condition within (source, subject). Unique among the open rows of that pair, so a repeated report touches this episode instead of adding another.';

COMMENT ON COLUMN signal_state.last_seen IS
    'The newest report; staleness is judged from here, never from opened_at.';

COMMENT ON COLUMN signal_state.resolved_at IS
    'NULL means open. A closed row is history: the next report of the same condition opens a new episode.';

COMMENT ON COLUMN signal_state.resolve_reason IS
    'Why the episode closed. Free text so a condition can name its own reason without a migration; the write path accepts base/events/signals/state.py:ResolveReason.';

COMMENT ON COLUMN signal_state.evidence_ref IS
    'What convinced the closer: a log line, an event id, a sample.';

COMMENT ON COLUMN signal_state.acked_by IS
    'Agent id that acknowledged the episode. An acknowledgement is not a close.';

-- One open episode per condition. Partial, because a resolved row must not block the next
-- episode of the same condition.
CREATE UNIQUE INDEX IF NOT EXISTS signal_state_open_key_idx
    ON signal_state (source, subject, condition)
    WHERE resolved_at IS NULL;

-- The reconciler's scan: the open episodes that have not reported in longest.
CREATE INDEX IF NOT EXISTS signal_state_open_last_seen_idx
    ON signal_state (last_seen)
    WHERE resolved_at IS NULL;
