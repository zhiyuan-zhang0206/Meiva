-- The capability-aware lease statements write two columns the runner's
-- column-level grant never listed (task #5375). `agent_impersonations` grants
-- the runner INSERT/UPDATE column by column on purpose: the table-level grant
-- would widen every other write. `fa4c8a241` added the columns to the request
-- INSERT and the renewal UPDATE without extending either copy of the list
-- (`db/schema.sql` and the birth-time convergence in
-- `base.cluster.authority.event_grants`), so `meiva impersonation request` and
-- `meiva impersonation renew` failed with InsufficientPrivilege from the
-- runner login while the gateway — which holds a table grant — kept working.
--
-- Only the columns a runner path writes are granted: `capabilities` is set once
-- at request time and no UPDATE path touches it; `capability_declared_by` is
-- restamped by the renewal for a pre-vocabulary lease.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='meiva_runner') THEN
        GRANT INSERT (capabilities, capability_declared_by)
            ON agent_impersonations TO meiva_runner;
        GRANT UPDATE (capability_declared_by)
            ON agent_impersonations TO meiva_runner;
    END IF;
END $$;
