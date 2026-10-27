-- A task can be silenced until an explicit deadline: the reminder and escalation passes skip it
-- while the deadline is in the future, and a deadline at or before now means "not paused", so the
-- column carries the whole state with no second flag (task #5311). IF NOT EXISTS because
-- db/schema.sql already declares the column: a fresh database builds from the baseline and then
-- replays the migrations it has not stamped.
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS reminders_paused_until TIMESTAMPTZ;
