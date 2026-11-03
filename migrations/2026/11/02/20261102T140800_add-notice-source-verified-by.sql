-- The credential fact that authenticated the request which wrote this notice.
--
-- Inbound messages already persist the same fact (`source_verified_by` on the
-- inbox row, values `cluster_bearer` / `machine_token:<class>` /
-- `user_session` / `machine_session:runner` / `trusted_network`); notices had
-- none, so "which credential posted this notice" could not be answered.
--
-- The value is the SERVER's record of the credential it verified, never a
-- caller's claim: the URL's agent_id is self-reported and is deliberately not
-- copied here. `internal` marks the in-process writers that present no
-- credential at all (e.g. task maintenance). NULL means a row written before
-- this column existed.
--
-- Idempotent: a fresh database builds this from db/schema.sql first and then
-- replays this file, and the migration-reset paths replay it against a database
-- whose schema already carries the column.
ALTER TABLE agent_notices
    ADD COLUMN IF NOT EXISTS source_verified_by TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'agent_notices_source_verified_by_length'
          AND conrelid = 'agent_notices'::regclass
    ) THEN
        ALTER TABLE agent_notices
            ADD CONSTRAINT agent_notices_source_verified_by_length
            CHECK (source_verified_by IS NULL OR length(source_verified_by) <= 120);
    END IF;
END
$$;
