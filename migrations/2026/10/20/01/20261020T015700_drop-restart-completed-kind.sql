-- 'restart_completed' has no writer (the respawn path that wrote it is retired);
-- remove the leftover rows and stop accepting the kind.
DELETE FROM inbound_messages WHERE kind = 'restart_completed';
ALTER TABLE inbound_messages DROP CONSTRAINT IF EXISTS inbound_messages_kind_check;
ALTER TABLE inbound_messages ADD CONSTRAINT inbound_messages_kind_check CHECK (kind IN (
    'chat', 'system_note', 'compact_summary', 'compact_request', 'cancel', 'terminate',
    'restart', 'resurrect', 'fork', 'heartbeat', 'reminder'
));
