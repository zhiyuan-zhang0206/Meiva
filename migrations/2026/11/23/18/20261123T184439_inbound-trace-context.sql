SET LOCAL lock_timeout = '5s';
ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS trace_context JSONB;
