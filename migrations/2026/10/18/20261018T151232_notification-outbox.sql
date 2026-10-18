-- Notification outbox: the delivery row is durable when the producer decides,
-- and only an accepted publication marks it delivered.
--
-- A row with `announced_at IS NULL` is still owed its live `notification` event.
-- `base/events/notifications/publish.py` marks the row only after the transport
-- accepted the event (a failed publish returns None and marks nothing), and the
-- gateway's outbox sweep re-announces rows no process announced -- a Redis
-- outage, or a crash between the producing commit and the publish.
--
-- Rows written before this column existed went through an announce path that
-- could not report a failure. They are marked delivered here instead of being
-- replayed by the first sweep: a month of history is not a backlog to push.
-- The backfill is guarded by the column's own birth, so re-running this file on
-- a live cluster cannot mark a row that is still owed as delivered.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'notification_events'
          AND column_name = 'announced_at'
    ) THEN
        ALTER TABLE notification_events ADD COLUMN announced_at TIMESTAMPTZ;
        UPDATE notification_events SET announced_at = created_at;
    END IF;
END $$;

COMMENT ON COLUMN notification_events.announced_at IS
    'When the live notification event was published; NULL means the delivery is still owed and the outbox sweep retries it.';

CREATE INDEX IF NOT EXISTS notification_events_pending_idx
    ON notification_events (created_at)
    WHERE announced_at IS NULL;

-- The runner announces its own `core.turn_finished` deliveries, so it marks too.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON notification_events TO meiva_runner;
    END IF;
END $$;
