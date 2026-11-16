-- Declarative, run-to-completion schedules. Existing resident schedules stay
-- on the daemon bridge until the later migration wave converts each payload.
ALTER TABLE schedules
    ADD COLUMN trigger_kind TEXT NOT NULL DEFAULT 'daemon',
    ADD COLUMN run_at TIMESTAMPTZ,
    ADD COLUMN cron_expr TEXT,
    ADD COLUMN interval_seconds INTEGER,
    ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC',
    ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 600,
    ADD COLUMN on_owner_terminate TEXT NOT NULL DEFAULT 'cancel';

UPDATE schedules
SET on_owner_terminate = 'cancel'
WHERE owner_agent_id IS NOT NULL;

ALTER TABLE schedules
    DROP CONSTRAINT schedules_status_check,
    ADD CONSTRAINT schedules_status_check
        CHECK (status IN ('scheduled', 'running', 'stopped', 'error', 'completed', 'cancelled')),
    ADD CONSTRAINT schedules_trigger_kind_check
        CHECK (trigger_kind IN ('at', 'cron', 'every', 'daemon')),
    ADD CONSTRAINT schedules_interval_seconds_check
        CHECK (interval_seconds IS NULL OR interval_seconds >= 30),
    ADD CONSTRAINT schedules_timeout_seconds_check
        CHECK (timeout_seconds > 0),
    ADD CONSTRAINT schedules_owner_terminate_check
        CHECK (on_owner_terminate IN ('cancel', 'keep')),
    ADD CONSTRAINT schedules_trigger_payload_check
        CHECK (
            (trigger_kind = 'daemon' AND run_at IS NULL AND cron_expr IS NULL AND interval_seconds IS NULL)
            OR (trigger_kind = 'at' AND run_at IS NOT NULL AND cron_expr IS NULL AND interval_seconds IS NULL)
            OR (trigger_kind = 'cron' AND run_at IS NULL AND cron_expr IS NOT NULL AND interval_seconds IS NULL)
            OR (trigger_kind = 'every' AND run_at IS NULL AND cron_expr IS NULL AND interval_seconds IS NOT NULL)
        );

CREATE INDEX schedules_declarative_due_idx
    ON schedules (machine, next_launch_at)
    WHERE enabled AND trigger_kind <> 'daemon';

ALTER TABLE schedule_runs
    ADD COLUMN scheduled_for TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION cancel_agent_schedules_on_termination()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'terminated' AND OLD.status IS DISTINCT FROM 'terminated' THEN
        UPDATE schedules
        SET enabled = FALSE,
            status = 'cancelled',
            next_launch_at = NULL,
            updated_at = now()
        WHERE owner_agent_id = NEW.id
          AND on_owner_terminate = 'cancel'
          AND status NOT IN ('completed', 'cancelled');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agents_meta_cancel_schedules
    AFTER UPDATE OF status ON agents_meta
    FOR EACH ROW
    EXECUTE FUNCTION cancel_agent_schedules_on_termination();

COMMENT ON COLUMN schedules.trigger_kind IS
    'Execution model: at/cron/every are declarative single-round triggers; daemon is the temporary resident-session compatibility bridge.';
COMMENT ON COLUMN schedules.owner_agent_id IS
    'Agent this schedule speaks as and whose workspace contains its declarative run files; NULL means a system-level schedule. The SDK/API may assign it and default placement to that agent''s machine.';
COMMENT ON COLUMN schedules.next_launch_at IS
    'The next declarative slot eligible to launch; NULL for completed/cancelled/disabled schedules and resident daemon schedules.';
COMMENT ON COLUMN schedules.on_owner_terminate IS
    'cancel disables unfinished work when its owner agent terminates; keep leaves it independent of owner lifecycle.';
COMMENT ON COLUMN schedule_runs.scheduled_for IS
    'Declarative slot claimed by this run; NULL for legacy resident-process runs.';
