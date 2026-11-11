-- Contract step for the notification and question rework. Every object dropped here has no
-- reader and no writer in the code that ships with this migration (the generation running
-- before it never touched them either):
--
--   * agent_user_messages.read_at and its partial index agent_user_messages_pending: unread
--     is the per-agent seen cursor (agent_user_seen) since 20261110T153500_agent-user-seen.
--   * notification_events.dedupe_key and its index: the push has been question + unread, with
--     a per-tag replace, since 20261111T041500_notification-action-and-tag; nothing
--     deduplicates by key any more.
--   * agent_notices.schedule_id (and its CHECK): the notice create body that reported it is
--     gone; the one writer left is the Ask endpoint, which never set it.
--   * agent_notices indexes for the removed Inbox and FYI feeds (unread, resolved history) and
--     for the expiry reaper that no longer exists; the open-question index loses its
--     `require_response` predicate because every unresolved row is an open question.
--
-- agent_notices.local_id, priority, require_response, blocking, updated_at, task_id and
-- expire_at are no longer read or written by the new code but stay for one deploy, since the
-- generation that is running still reads them; a later migration drops them. Until then the
-- Ask insert omits them, so the ones that were NOT NULL without a default are relaxed.
-- Idempotent: a fresh database replays this over a schema that already lacks the objects.
DROP INDEX IF EXISTS agent_user_messages_pending;
ALTER TABLE agent_user_messages DROP COLUMN IF EXISTS read_at;

DROP INDEX IF EXISTS notification_events_dedupe_idx;
ALTER TABLE notification_events DROP COLUMN IF EXISTS dedupe_key;

ALTER TABLE agent_notices DROP COLUMN IF EXISTS schedule_id;
DROP INDEX IF EXISTS agent_notices_unread_idx;
DROP INDEX IF EXISTS agent_notices_resolved_idx;
DROP INDEX IF EXISTS agent_notices_expire_at_idx;
DROP INDEX IF EXISTS agent_notices_awaiting_idx;
CREATE INDEX IF NOT EXISTS agent_notices_open_idx
    ON agent_notices (agent_id, created_at)
    WHERE resolved_at IS NULL;

ALTER TABLE agent_notices
    ALTER COLUMN local_id DROP NOT NULL,
    ALTER COLUMN priority SET DEFAULT 'P2',
    ALTER COLUMN require_response SET DEFAULT TRUE;
