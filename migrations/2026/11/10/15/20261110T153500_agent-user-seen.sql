-- One monotonic "seen up to" cursor per agent replaces per-record read state. Unread means a
-- delivery with id above the cursor. The pending index and agent_user_messages.read_at stop
-- being read or written here; a later migration drops them (expand-contract).
CREATE TABLE IF NOT EXISTS agent_user_seen (
    agent_id BIGINT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    seen_id BIGINT NOT NULL CHECK (seen_id >= 0)
);
COMMENT ON TABLE agent_user_seen IS 'The user has seen deliveries (agent_user_messages.id) up to seen_id; no row means none.';
CREATE INDEX IF NOT EXISTS agent_user_messages_by_agent ON agent_user_messages (agent_id, id)
    WHERE content IS NOT NULL;

-- Keep each agent's existing unread state: the cursor stops just before its oldest unread record,
-- or at its newest record when everything was read.
INSERT INTO agent_user_seen (agent_id, seen_id)
SELECT agent_id,
       COALESCE(MIN(id) FILTER (WHERE read_at IS NULL AND content IS NOT NULL) - 1, MAX(id))
FROM agent_user_messages
GROUP BY agent_id
ON CONFLICT (agent_id) DO NOTHING;
