-- The skill trust tier is deleted: nothing reads or writes extensions.trust any more, and
-- the CHECK on the column goes with it. Lossy, deployed with a full stop of the gateway and
-- every runner, so no build that still names the column runs against the new schema.
-- Idempotent: a fresh database replays this over a schema that already lacks the column.
ALTER TABLE extensions DROP COLUMN IF EXISTS trust;
