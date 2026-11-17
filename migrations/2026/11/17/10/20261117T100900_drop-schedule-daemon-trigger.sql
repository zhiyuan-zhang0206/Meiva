-- Resident schedules have been retired. Refuse an upgrade with unconverted rows
-- instead of deleting their payloads and run history.
ALTER TABLE schedules
    ALTER COLUMN trigger_kind DROP DEFAULT,
    DROP CONSTRAINT IF EXISTS schedules_trigger_kind_check,
    DROP CONSTRAINT IF EXISTS schedules_trigger_payload_check,
    DROP CONSTRAINT IF EXISTS schedules_check,
    ADD CONSTRAINT schedules_trigger_kind_check
        CHECK (trigger_kind IN ('at', 'cron', 'every')),
    ADD CONSTRAINT schedules_trigger_payload_check
        CHECK (
            (trigger_kind = 'at' AND run_at IS NOT NULL AND cron_expr IS NULL AND interval_seconds IS NULL)
            OR (trigger_kind = 'cron' AND run_at IS NULL AND cron_expr IS NOT NULL AND interval_seconds IS NULL)
            OR (trigger_kind = 'every' AND run_at IS NULL AND cron_expr IS NULL AND interval_seconds IS NOT NULL)
        );

DROP INDEX IF EXISTS schedules_declarative_due_idx;
CREATE INDEX schedules_declarative_due_idx ON schedules (machine, next_launch_at)
    WHERE enabled;

COMMENT ON COLUMN schedules.trigger_kind IS
    'Execution model: at/cron/every are declarative single-round triggers.';
COMMENT ON COLUMN schedules.next_launch_at IS
    'The next declarative slot eligible to launch; NULL for completed/cancelled/disabled schedules.';
