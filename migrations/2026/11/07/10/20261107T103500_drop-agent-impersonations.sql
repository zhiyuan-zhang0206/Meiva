-- Remove legacy impersonation (takeover lease) subsystem

-- 1. Drop triggers
DROP TRIGGER IF EXISTS inbound_messages_impersonation_history ON inbound_messages;
DROP TRIGGER IF EXISTS agents_meta_revoke_impersonation ON agents_meta;
DROP TRIGGER IF EXISTS agent_termination_revokes_impersonation ON agents_meta;
DROP TRIGGER IF EXISTS agent_impersonations_restore_native_owner ON agent_impersonations;
DROP TRIGGER IF EXISTS agent_impersonations_allocate ON agent_impersonations;
DROP TRIGGER IF EXISTS agent_impersonations_preserve_history ON agent_impersonations;
DROP TRIGGER IF EXISTS agent_impersonation_entries_preserve_history ON agent_impersonation_entries;
DROP TRIGGER IF EXISTS agent_impersonations_finalize_event_log ON agent_impersonations;
DROP TRIGGER IF EXISTS agent_impersonation_entries_guard_source ON agent_impersonation_entries;
DROP TRIGGER IF EXISTS agent_impersonations_lifecycle ON agent_impersonations;
DROP TRIGGER IF EXISTS agent_impersonations_terminal_notice ON agent_impersonations;
DROP TRIGGER IF EXISTS agent_impersonations_require_meiva ON agent_impersonations;

-- 2. Drop functions
DROP FUNCTION IF EXISTS record_impersonation_inbound();
DROP FUNCTION IF EXISTS record_impersonation_lifecycle();
DROP FUNCTION IF EXISTS mark_impersonation_terminal_notice();
DROP FUNCTION IF EXISTS require_meiva_impersonation();
DROP FUNCTION IF EXISTS guard_impersonation_event_source();
DROP FUNCTION IF EXISTS revoke_terminated_impersonation();
DROP FUNCTION IF EXISTS revoke_impersonation_on_termination();
DROP FUNCTION IF EXISTS restore_native_impersonation_owner();
DROP FUNCTION IF EXISTS allocate_impersonation_session();
DROP FUNCTION IF EXISTS preserve_impersonation_history();
DROP FUNCTION IF EXISTS close_impersonation_event_admission(UUID);
DROP FUNCTION IF EXISTS seal_impersonation_event_participant(UUID, TEXT, TEXT, TEXT, BIGINT);
DROP FUNCTION IF EXISTS lock_impersonation_event_participant(UUID, TEXT);
DROP FUNCTION IF EXISTS finalize_impersonation_event_log(UUID);
DROP FUNCTION IF EXISTS finalize_impersonation_event_log_on_end();

-- 3. Drop tables
DROP TABLE IF EXISTS agent_impersonation_messages CASCADE;
DROP TABLE IF EXISTS agent_impersonation_entries CASCADE;
DROP TABLE IF EXISTS agent_impersonation_event_participants CASCADE;
DROP TABLE IF EXISTS agent_impersonation_sources CASCADE;
DROP TABLE IF EXISTS agent_impersonations CASCADE;

-- 4. Drop column on agents
ALTER TABLE agents DROP COLUMN IF EXISTS impersonation_index;

-- 5. Drop 'reminder' from inbound_messages.kind CHECK constraint
ALTER TABLE public.inbound_messages DROP CONSTRAINT IF EXISTS inbound_messages_kind_check;

ALTER TABLE public.inbound_messages
    ADD CONSTRAINT inbound_messages_kind_check
    CHECK (kind IN (
        'chat',
        'system_note',
        'compact_summary',
        'compact_request',
        'cancel',
        'terminate',
        'restart',
        'resurrect',
        'fork',
        'heartbeat'
    ));
