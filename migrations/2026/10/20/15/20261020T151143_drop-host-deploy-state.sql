-- Drop the per-host deploy posture table. No lifecycle command reads it for a
-- decision any more: a stop, restart and start leave their evidence in the home's
-- lifecycle journal, and the status endpoints answer from the serving gate.
-- Deployed with the code that stops using the table (a stopped cluster), so
-- nothing here is expand-contract.

DROP TABLE IF EXISTS host_deploy_state;
