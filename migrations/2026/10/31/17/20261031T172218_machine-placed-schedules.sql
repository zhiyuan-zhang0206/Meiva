-- Machine-placed schedules and schedule-manager runner execution (Slice 1).
--
-- Adds machine identity, plugin attribution, and per-schedule secret injection
-- to schedules; partitions schedule_sync_requests by (schedule_id, machine); and
-- grants meiva_runner the least privileges needed to execute schedules on
-- any cluster machine.

ALTER TABLE schedules
    ADD COLUMN machine TEXT,
    ADD COLUMN owner_plugin TEXT CONSTRAINT schedules_owner_plugin_nonempty CHECK (owner_plugin IS NULL OR btrim(owner_plugin) <> ''),
    ADD COLUMN secret_env_vars TEXT[] NOT NULL DEFAULT '{}';

UPDATE schedules
SET machine = COALESCE(
    (SELECT machine_name FROM machine_units WHERE serve_gateway ORDER BY up_since_at DESC NULLS LAST LIMIT 1),
    (SELECT name FROM machines WHERE 'gateway' = ANY(role) ORDER BY up_since_at DESC NULLS LAST LIMIT 1),
    'gateway'
);

ALTER TABLE schedules
    ALTER COLUMN machine SET NOT NULL,
    ADD CONSTRAINT schedules_machine_nonempty CHECK (btrim(machine) <> '');

COMMENT ON COLUMN schedules.machine IS
    'Name of the machine assigned to execute this schedule. A machine''s local schedule-manager executes only schedules matching its machine name.';

COMMENT ON COLUMN schedules.owner_plugin IS
    'Plugin directory name that owns and declared this schedule; NULL for operator-created and core builtin schedules.';

COMMENT ON COLUMN schedules.secret_env_vars IS
    'Environment variable names injected into this schedule''s session at launch.';

CREATE INDEX schedules_machine_enabled_idx ON schedules (machine, enabled);
CREATE INDEX schedules_owner_plugin_idx ON schedules (owner_plugin) WHERE owner_plugin IS NOT NULL;

-- Partition schedule_sync_requests by machine so multi-machine convergence requests
-- (such as moving a schedule from one machine to another) can be queued and consumed
-- independently per machine.
DELETE FROM schedule_sync_requests WHERE schedule_id NOT IN (SELECT id FROM schedules);

ALTER TABLE schedule_sync_requests
    ADD COLUMN machine TEXT;

UPDATE schedule_sync_requests s
SET machine = (SELECT m.machine FROM schedules m WHERE m.id = s.schedule_id);

ALTER TABLE schedule_sync_requests
    ALTER COLUMN machine SET NOT NULL,
    ADD CONSTRAINT schedule_sync_requests_machine_nonempty CHECK (btrim(machine) <> ''),
    DROP CONSTRAINT schedule_sync_requests_pkey,
    ADD PRIMARY KEY (schedule_id, machine);

COMMENT ON COLUMN schedule_sync_requests.machine IS
    'Target machine where the schedule convergence or session reaping should be performed.';

-- meiva_runner surface: schedule-manager runs as meiva_runner on every machine
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT, UPDATE ON schedules TO meiva_runner;
        GRANT SELECT, INSERT, UPDATE ON schedule_runs TO meiva_runner;
        GRANT SELECT, DELETE ON schedule_sync_requests TO meiva_runner;
        GRANT SELECT, INSERT ON schedule_fire_log TO meiva_runner;
        GRANT USAGE, SELECT ON SEQUENCE schedule_runs_id_seq TO meiva_runner;
        GRANT USAGE, SELECT ON SEQUENCE schedule_fire_log_id_seq TO meiva_runner;
    END IF;
END $$;
