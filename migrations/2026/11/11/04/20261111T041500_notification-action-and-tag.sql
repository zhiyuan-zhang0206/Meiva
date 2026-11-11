-- A delivery now says what a client does with it and which on-screen notification it concerns:
-- `action` raises it (alert), swaps the content of the one already shown without a sound
-- (replace) or takes it away (retract); `tag` is the stable identity of that notification
-- (`unread:<agent id>` or `question:<notice id>`). Rows written before this migration were
-- one-shot alerts with no identity: they keep the empty tag, which a client skips.
-- `dedupe_key` and its index stay until a later contract migration.
ALTER TABLE notification_events
    ADD COLUMN IF NOT EXISTS action TEXT NOT NULL DEFAULT 'alert'
        CHECK (action IN ('alert', 'replace', 'retract')),
    ADD COLUMN IF NOT EXISTS tag TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN notification_events.action IS
    'What a client does with the delivery: alert (raise it), replace (swap the content of the shown notification without a sound) or retract (take it away).';
COMMENT ON COLUMN notification_events.tag IS
    'Stable identity of the on-screen notification this delivery concerns: unread:<agent id> or question:<notice id>. Empty on rows written before tags existed.';
