-- Notification spine: one row per delivered user notification.
-- This is a delivery log, not an inbox. Rows are never answered, resolved or
-- superseded; `agent_notices` remains the inbox. A transport renders from these
-- rows so no client re-derives its own trigger from agent state.
CREATE TABLE IF NOT EXISTS notification_events (
    id              BIGSERIAL PRIMARY KEY,
    category        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT,
    href            TEXT,
    -- Deduplication identity. NULL opts the category out of deduplication.
    dedupe_key      TEXT,
    importance      TEXT NOT NULL CHECK (importance IN ('low', 'normal', 'high')),
    -- Producing agent, or 0 for a system-level notification.
    agent_id        BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS notification_events_created_idx
    ON notification_events (created_at DESC);

-- The dedupe lookup: the newest delivery for one (category, dedupe_key).
CREATE INDEX IF NOT EXISTS notification_events_dedupe_idx
    ON notification_events (category, dedupe_key, created_at DESC)
    WHERE dedupe_key IS NOT NULL;
