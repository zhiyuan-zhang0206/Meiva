CREATE OR REPLACE FUNCTION cancel_agent_schedules_on_termination()
RETURNS TRIGGER AS $$
DECLARE
    cancelled_schedules TEXT;
BEGIN
    IF NEW.status = 'terminated' AND OLD.status IS DISTINCT FROM 'terminated' THEN
        -- Restart intent is local to the releasing transaction, including
        -- savepoints; the terminate command remains the auto-resurrection fence.
        IF NEW.termination_source = 'user'
           AND current_setting('meiva.lifecycle_release', true) = 'restart' THEN
            RETURN NEW;
        END IF;

        WITH cancelled AS (
            UPDATE schedules
            SET enabled = FALSE,
                status = 'cancelled',
                next_launch_at = NULL,
                updated_at = now()
            WHERE owner_agent_id = NEW.id
              AND on_owner_terminate = 'cancel'
              AND status NOT IN ('completed', 'cancelled')
            RETURNING id, name
        )
        SELECT string_agg(format('- #%s: %s', id, name), E'\n' ORDER BY id)
        INTO cancelled_schedules FROM cancelled;

        IF cancelled_schedules IS NOT NULL THEN
            INSERT INTO inbound_messages (agent_id, content, kind, source, payload)
            VALUES (
                NEW.id,
                'Owner termination cancelled these schedules '
                || '(on_owner_terminate=cancel):' || E'\n' || cancelled_schedules,
                'system_note', 'system:schedules', '{"passive": true}'::jsonb
            );
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
