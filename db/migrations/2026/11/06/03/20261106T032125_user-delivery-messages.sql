CREATE TABLE IF NOT EXISTS agent_user_messages (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('naive', 'prefix', 'sdk')),
    content TEXT,
    suggestions JSONB NOT NULL CHECK (jsonb_typeof(suggestions) = 'array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    UNIQUE (agent_id, source_key),
    CHECK (content IS NULL OR length(btrim(content)) > 0),
    CHECK (content IS NOT NULL OR suggestions = '[]'::jsonb)
);
CREATE INDEX IF NOT EXISTS agent_user_messages_pending ON agent_user_messages (agent_id, id)
    WHERE content IS NOT NULL AND read_at IS NULL;

-- Pending questions retain their identity and answers while the UI adopts Ask.
ALTER TABLE agent_notices ALTER COLUMN expire_at DROP NOT NULL;
UPDATE agent_notices SET expire_at=NULL, question=COALESCE(question, '{"type":"text"}'::jsonb) WHERE require_response AND resolved_at IS NULL;

CREATE TABLE reply_suggestion_offers (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    context TEXT NOT NULL,
    suggestions JSONB NOT NULL CHECK (jsonb_typeof(suggestions)='array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE reply_suggestion_feedback (
    id BIGSERIAL PRIMARY KEY,
    offer_id BIGINT NOT NULL REFERENCES reply_suggestion_offers(id) ON DELETE CASCADE,
    candidate TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted','edited','rejected')),
    sent_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((outcome='rejected') = (sent_text IS NULL))
);
