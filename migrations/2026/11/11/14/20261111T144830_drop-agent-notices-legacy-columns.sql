-- Contract step for the notification and question rework (20261111T080521_drop-dead-notice-storage
-- was the expand step). agent_notices.local_id, priority, require_response, blocking, updated_at,
-- task_id and expire_at have no reader and no writer in the code that ships with this migration,
-- and none in the generation that ran before it (the code that stopped touching them): the only
-- INSERT is the Ask endpoint, which names (agent_id, title, content, question, source_verified_by),
-- and the only UPDATE sets (resolved_at, resolution, reply, answer). Lossy, so it applies only once
-- the expand step is deployed.
--
-- Constraints that existed for those columns go with them:
--   * agent_notices_agent_local_id_unique (agent_id, local_id)
--   * agent_notices_blocking_requires_response
--   * agent_notices_resolution_legal: it tied the resolution values to require_response; the
--     column CHECK on `resolution` still admits the historical values read, withdrawn,
--     superseded and expired, which old rows carry.
--   * agent_notices_task_id_fkey
--   * agent_notices_question_requires_response: its other half guarded the question payload
--     shape, which stays as agent_notices_question_is_object.
--
-- schedule_id is dropped again here (already gone where 20261111T080521 ran): a fresh database
-- stamps that migration as folded but still replays 20261108T211435_add-notice-schedule-id,
-- which would otherwise leave the column behind.
-- Idempotent: a fresh database replays this over a schema that already lacks the objects.
ALTER TABLE agent_notices
    DROP CONSTRAINT IF EXISTS agent_notices_question_requires_response,
    DROP CONSTRAINT IF EXISTS agent_notices_agent_local_id_unique,
    DROP CONSTRAINT IF EXISTS agent_notices_blocking_requires_response,
    DROP CONSTRAINT IF EXISTS agent_notices_resolution_legal,
    DROP CONSTRAINT IF EXISTS agent_notices_task_id_fkey;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'agent_notices_question_is_object'
          AND conrelid = 'agent_notices'::regclass
    ) THEN
        ALTER TABLE agent_notices
            ADD CONSTRAINT agent_notices_question_is_object
            CHECK (question IS NULL OR jsonb_typeof(question) = 'object');
    END IF;
END
$$;

ALTER TABLE agent_notices
    DROP COLUMN IF EXISTS schedule_id,
    DROP COLUMN IF EXISTS local_id,
    DROP COLUMN IF EXISTS priority,
    DROP COLUMN IF EXISTS require_response,
    DROP COLUMN IF EXISTS blocking,
    DROP COLUMN IF EXISTS updated_at,
    DROP COLUMN IF EXISTS task_id,
    DROP COLUMN IF EXISTS expire_at;
