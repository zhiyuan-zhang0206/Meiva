-- A turn is interrupted, not cancelled: the inbound kind `cancel` becomes `interrupt`.
-- Existing rows keep their meaning; the constraint is replaced with the renamed value.

ALTER TABLE inbound_messages DROP CONSTRAINT IF EXISTS inbound_messages_kind_check;

UPDATE inbound_messages SET kind = 'interrupt' WHERE kind = 'cancel';

ALTER TABLE inbound_messages ADD CONSTRAINT inbound_messages_kind_check CHECK (kind IN (
    'chat',
    'system_note',
    'compact_summary',
    'compact_request',
    'interrupt',
    'terminate',
    'restart',
    'resurrect',
    'fork',
    'heartbeat'
));
