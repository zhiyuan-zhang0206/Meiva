-- The timeline item of a CLI tool call is found by the CLI's own id: the transcript writer asks for it when a
-- sub-agent's output arrives whose spawning call is in an earlier batch or an earlier process. Without an index
-- every such lookup scans the agent's whole timeline.
--
-- The id is a column of its own (call_id), not an index over an expression of `item`: an index that references
-- `item` stops every in-place revision of a live activity (UPDATE ... SET item) from being a heap-only update, on
-- every row of the table, whether or not the row is a call. The column is NULL except for `agent_code` items with a
-- non-empty tool_use_id, which every writer fills on insert, and an in-place revision never changes it, so the update
-- stays heap-only. An earlier version of this migration built an expression index under the name
-- idx_agent_harness_messages_call; the new index has another name and that one is dropped first, so a database that ran
-- the earlier version does not keep it (a CREATE INDEX IF NOT EXISTS under the old name would have skipped silently).
--
-- Expand step, in four statements. Idempotent, so a fresh database that already has them replays it.
--
-- Cost and locking: this runs only in the migration window of `meiva start`, where the services that write the
-- table are stopped, so nothing waits on it. The backfill is done in batches of 5000 rows by id range (one transaction
-- like every migration) and touches only `agent_code` items with a non-empty tool_use_id; each such row gets a new
-- row version, so the table grows by about a third on a large one until a later vacuum reclaims it. The plain
-- (not CONCURRENTLY) CREATE INDEX blocks writes while it builds, accepted for the same reason.
--
-- Precondition: after this migration is applied no process of an older version may write the table. A row an older
-- process writes has a NULL call_id, so a sub-agent's output could not find its parent call. The update flow stops
-- every service before it starts the new version, which satisfies this.
ALTER TABLE agent_harness_messages ADD COLUMN IF NOT EXISTS call_id TEXT;

DO $$
DECLARE
    batch_start BIGINT;
    max_id BIGINT;
BEGIN
    SELECT COALESCE(MIN(id), 0), COALESCE(MAX(id), -1) INTO batch_start, max_id FROM agent_harness_messages;
    WHILE batch_start <= max_id LOOP
        UPDATE agent_harness_messages
           SET call_id = item->>'tool_use_id'
         WHERE id >= batch_start AND id < batch_start + 5000
           AND call_id IS NULL
           AND item->>'kind' = 'agent_code'
           AND COALESCE(item->>'tool_use_id', '') <> '';
        batch_start := batch_start + 5000;
    END LOOP;
END $$;

DROP INDEX IF EXISTS idx_agent_harness_messages_call;

CREATE INDEX IF NOT EXISTS idx_agent_harness_messages_call_id
    ON agent_harness_messages (agent_id, call_id) WHERE call_id IS NOT NULL;

COMMENT ON COLUMN agent_harness_messages.call_id IS
    'The CLI''s id of the tool call an agent_code item recorded (the item''s tool_use_id); NULL for every other item. Indexed, so a sub-agent''s output finds the item it nests under; never changed by an in-place revision, so those stay heap-only updates.';
