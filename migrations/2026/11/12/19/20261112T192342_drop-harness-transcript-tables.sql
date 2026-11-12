-- Drop the two tables of the stored-transcript timeline writer, which no code reads or writes any more
-- (docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md: an external agent's timeline is read
-- from the CLI's own record when it is asked for, and stored nowhere). Created by
-- 20261112T101810_harness-timeline-source-key.sql behind the writer's agent-level rollout setting, which defaulted
-- to off and was never turned on in any deployment, so neither table has ever held a row; this is not a lossy change.
-- That is why the usual expand-contract pair (stop using in one release, drop in a later one) is one step here: the
-- code that used them is deleted in the same change, and a binary that still has it never wrote to them.
-- Idempotent: a fresh database built from db/schema.sql has neither table, and the earlier migration's
-- CREATE TABLE IF NOT EXISTS recreates them for the replay that this statement removes again. The grants and the
-- indexes go with the tables.
DROP TABLE IF EXISTS agent_harness_transcript_rows;
DROP TABLE IF EXISTS agent_harness_transcript_positions;
