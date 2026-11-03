-- Owner assignment becomes a provisioning fact, and each change records why (task #6102).
--
-- The API no longer accepts `owner_agent_id` (create or update). Whoever a schedule speaks as
-- decides whose name its unattended writes carry, and the gateway cannot tell callers apart -- one
-- shared administrator credential, caller labels deliberately absent -- so an API caller could
-- otherwise hand a schedule another agent's name and have it run under that name. The operator path
-- is `scripts/host_ops/set_schedule_owner.py`, run on the gateway host with the home's database;
-- the reason they type is the only evidence this change will ever have.
--
-- This table is that evidence: one row per assignment, with the old and new owner, who ran it
-- (`ops:<hostname>`), and why. Append-only; the schedules row carries only the current value.
CREATE TABLE IF NOT EXISTS schedule_owner_changes (
    id           BIGSERIAL PRIMARY KEY,
    schedule_id  BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    old_owner_id BIGINT REFERENCES agents(id),
    new_owner_id BIGINT REFERENCES agents(id),
    source       TEXT NOT NULL CONSTRAINT schedule_owner_changes_source_nonempty CHECK (btrim(source) <> ''),
    reason       TEXT NOT NULL CONSTRAINT schedule_owner_changes_reason_nonempty CHECK (btrim(reason) <> ''),
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS schedule_owner_changes_schedule_idx
    ON schedule_owner_changes (schedule_id, changed_at DESC);

COMMENT ON TABLE schedule_owner_changes IS
    'Append-only record of owner assignments made through scripts/host_ops/set_schedule_owner.py: old and new owner, the operator host that ran it, the reason typed, and when. The API cannot change an owner, so this is the only audit surface for one.';
COMMENT ON COLUMN schedule_owner_changes.source IS
    'Who made the assignment: `ops:<hostname>` from the machine that ran the provisioning tool.';

-- Correct the earlier claim. `owns_loop=false` refuses `meiva.self.*` only: the schedule cannot
-- end, restart or compact its own loop. A cross-agent call such as `meiva.agents.terminate(<owner>)`
-- is an ordinary peer action and stays available, exactly as it is for any process holding the
-- cluster token.
COMMENT ON COLUMN schedules.owner_agent_id IS
    'Agent this schedule speaks as; NULL for an operator-created schedule that only runs a command. Assigned only through provisioning (scripts/host_ops/set_schedule_owner.py), never through the API. Written into the launched session''s MEIVA_AGENT_ID and into the runner''s bound AgentIdentity, and stamped on schedule_runs.agent_id. Not a privilege boundary: the session token is the cluster agent token either way, and the owner grants no authority a shell session lacks; it decides only who the schedule speaks as.';
