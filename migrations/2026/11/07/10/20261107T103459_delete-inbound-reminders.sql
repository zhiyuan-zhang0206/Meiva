-- Clean legacy inbound messages with retired kinds (such as 'reminder')
-- prior to constraint tightening in 20261107T103500_drop-agent-impersonations.

DELETE FROM public.inbound_messages
WHERE kind NOT IN (
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
);
