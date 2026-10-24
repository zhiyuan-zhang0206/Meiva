-- Record how an external harness session ended: the exit status of the CLI child
-- process, so "finished the turn and exited" and "died" stop looking identical in
-- the agent row. Task #5616.
--
-- Pure expand: both columns are nullable, carry no default and nothing is
-- backfilled, so a checkout that predates this migration keeps working against a
-- migrated database and vice versa. db/schema.sql carries the same columns and
-- comments.
--
-- Three states, and only three (the comment on the column is the single place they
-- are stated): NULL = still running, or never recorded — the built-in meiva loop
-- has no child process, so it must stay NULL rather than be faked to 0 — 0 = the
-- child exited normally, non-zero = it exited abnormally.

ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS harness_exit_code INTEGER;
ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS harness_exit_at TIMESTAMPTZ;

COMMENT ON COLUMN agents_meta.harness_exit_code IS
    'Exit status of the external harness CLI child for this agent''s most recent '
    'session: NULL = still running or never recorded (the built-in meiva loop has '
    'no child and records nothing), 0 = the child exited normally, non-zero = it '
    'exited abnormally.';
COMMENT ON COLUMN agents_meta.harness_exit_at IS
    'When harness_exit_code was written: the moment the session was retired, after '
    'the child was reaped. NULL whenever harness_exit_code is NULL.';
