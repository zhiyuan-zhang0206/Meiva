CREATE OR REPLACE FUNCTION cancel_agent_schedules_on_termination()
RETURNS TRIGGER AS $$
DECLARE
    cancelled_schedules TEXT;
BEGIN
    IF NEW.status = 'terminated' AND OLD.status IS DISTINCT FROM 'terminated' THEN
        -- A preserving restart releases its runtime through terminated before
        -- resurrection. Only its executed command in this transaction exempts
        -- it; an older restart must never exempt a later death or reaper.
        IF NEW.termination_source = 'user' AND EXISTS (
            SELECT 1 FROM inbound_messages c
            WHERE c.agent_id = NEW.id
              AND c.id = (
                  SELECT max(id) FROM inbound_messages
                  WHERE agent_id = NEW.id AND kind IN ('restart', 'terminate')
              )
              AND c.kind = 'restart' AND c.status = 'done'
              AND c.xmin = pg_current_xact_id()::xid
        ) THEN
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
