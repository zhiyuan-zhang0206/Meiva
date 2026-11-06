CREATE TABLE IF NOT EXISTS agent_user_messages (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('naive', 'prefix', 'sdk')),
    content TEXT,
    response TEXT NOT NULL CHECK (response IN ('none', 'optional', 'required')),
    suggestions JSONB NOT NULL CHECK (jsonb_typeof(suggestions) = 'array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    resolution TEXT CHECK (resolution IN ('answered', 'dismissed')),
    reply TEXT,
    UNIQUE (agent_id, source_key),
    CHECK (content IS NULL OR length(btrim(content)) > 0),
    CHECK (content IS NOT NULL OR (response = 'none' AND suggestions = '[]'::jsonb)),
    CHECK (response <> 'none' OR suggestions = '[]'::jsonb)
);
CREATE INDEX IF NOT EXISTS agent_user_messages_pending ON agent_user_messages (agent_id, id)
    WHERE content IS NOT NULL AND (read_at IS NULL OR (response = 'required' AND resolved_at IS NULL));
