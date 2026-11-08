-- The schedule whose run posted this notice, when its writer knows one.
--
-- `source_verified_by` says WHICH CREDENTIAL wrote the row; it cannot say which
-- schedule did. A process the schedule runner started carries that id in
-- `MEIVA_SCHEDULE_ID`, and the notice create body now reports it, so a notice a
-- schedule posted can be traced back to the schedule row and to the run that
-- fired the slot.
--
-- The caller reports the value and the server refuses one that names no
-- schedule; it is provenance, not identity, and never proves who called.
--
-- NULL means "not known": an agent's own notice, a manual run, or a row written
-- before this column existed. Idempotent for the fresh-database and
-- migration-reset paths, which replay this file over a schema that already
-- carries the column.
ALTER TABLE agent_notices
    ADD COLUMN IF NOT EXISTS schedule_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'agent_notices_schedule_id_positive'
          AND conrelid = 'agent_notices'::regclass
    ) THEN
        ALTER TABLE agent_notices
            ADD CONSTRAINT agent_notices_schedule_id_positive
            CHECK (schedule_id IS NULL OR schedule_id > 0);
    END IF;
END
$$;
