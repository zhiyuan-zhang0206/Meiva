-- The agent a schedule speaks as (task #6102).
--
-- A schedule process carried no agent identity, so a schedule whose script calls
-- `meiva.tasks` (or any other namespace registered per identity) failed at its first write,
-- and every message it sent was attributed to the placeholder actor `schedule:<id>` alone.
-- This column names the agent the schedule acts for; the runner writes it into the launched
-- session's `MEIVA_AGENT_ID` (command schedules) and into its own bound `AgentIdentity`
-- (.py schedules run in-process and never read the environment), and stamps it on each
-- `schedule_runs` row.
--
-- NULL is the operator-created, command-only schedule: it keeps exactly today's behavior.
-- No backfill in this migration — the column is structure; choosing an owner per schedule is
-- a separate, reviewable step.
--
-- Not a privilege boundary. The session still holds the cluster's agent API token, so an
-- owner grants nothing it did not already have; this column only decides *who the schedule
-- speaks as* and *what it is allowed to speak*. A schedule with an owner runs as a launched
-- script (`owns_loop` false): it cannot compact, restart or terminate the agent it speaks for.
ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS owner_agent_id BIGINT REFERENCES agents(id);

COMMENT ON COLUMN schedules.owner_agent_id IS
    'Agent this schedule speaks as; NULL for an operator-created schedule that only runs a command. Written into the launched session''s MEIVA_AGENT_ID and into the runner''s bound AgentIdentity, and stamped on schedule_runs.agent_id. Not a privilege boundary: the session token is the cluster agent token either way.';
