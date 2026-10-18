-- Drop the last leftover of the retired watcher storage.
--
-- `agents_meta.watchers` (jsonb NOT NULL DEFAULT '[]'::jsonb) is a long-deployed
-- column that no code has read or written since watcher restart was retired: the
-- 2026-10-01 contract (20261001T055030_drop-retired-deploy-and-watcher-storage.sql)
-- dropped the agent_watchers registry and agents_meta.closed_at but missed this
-- column, and no migration ever created it — it exists only on long-lived
-- deployments, never in db/schema.sql. Every row it holds today is '[]', so
-- nothing is carried anywhere; a fresh database (built from the baseline) has no
-- such column and the guarded drop is a no-op there.
--
-- Compatibility window: one release. Dropping a column no process references
-- needs no expand step; the code that could have used it shipped long ago.

ALTER TABLE agents_meta DROP COLUMN IF EXISTS watchers;
