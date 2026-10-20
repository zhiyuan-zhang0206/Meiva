-- Retire the storage of the host fencing the agent host no longer performs: one host
-- per machine, one UPDATE to admit a turn, restart/terminate executed and closed in
-- one transaction. Deployed with the code that stops using these objects (a stopped
-- cluster), so nothing here is expand-contract.
--
--   * the lifecycle pointer and its done guard, the inbound target/applied/observed
--     columns, and the keyed restart/cancel/compact receipt tables with their
--     projection trigger and the native work identity;
--   * the per-agent lease, runtime kind, protocol version, pid/start stamps and the
--     versioned resource evidence;
--   * the resurrection epoch and force-terminate fences, and the admission outcome.
--
-- `last_admission_at` stays: it is the "admitted at least once" fact of creation.
-- The state these objects held is derived again on the next admission.

DROP TRIGGER IF EXISTS native_restart_source_projection ON inbound_messages;
DROP FUNCTION IF EXISTS project_native_restart_source();
DROP TRIGGER IF EXISTS inbound_messages_lifecycle_pointer_done_guard ON inbound_messages;
DROP FUNCTION IF EXISTS reject_inbound_done_with_lifecycle_pointer();

DROP TABLE IF EXISTS native_restart_commands;
DROP TABLE IF EXISTS native_cancel_commands;
DROP TABLE IF EXISTS native_compact_commands;
DROP TABLE IF EXISTS native_compact_observations;
DROP TABLE IF EXISTS native_graph_work;

ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_lifecycle_command_fk;
ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_admission_observation_pair_check;
ALTER TABLE inbound_messages DROP CONSTRAINT IF EXISTS inbound_lifecycle_target_check;

ALTER TABLE agents_meta
    DROP COLUMN IF EXISTS lifecycle_command_id,
    DROP COLUMN IF EXISTS native_work_id,
    DROP COLUMN IF EXISTS incarnation_resources,
    DROP COLUMN IF EXISTS lease_expires_at,
    DROP COLUMN IF EXISTS runtime_kind,
    DROP COLUMN IF EXISTS runtime_protocol_version,
    DROP COLUMN IF EXISTS pid,
    DROP COLUMN IF EXISTS started_at,
    DROP COLUMN IF EXISTS last_admission_outcome,
    DROP COLUMN IF EXISTS last_resurrect_inbound_id,
    DROP COLUMN IF EXISTS last_force_terminate_inbound_id;

ALTER TABLE inbound_messages
    DROP COLUMN IF EXISTS target_generation,
    DROP COLUMN IF EXISTS target_owner,
    DROP COLUMN IF EXISTS applied_at,
    DROP COLUMN IF EXISTS observed_at;
