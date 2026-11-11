-- External-harness delivery has one "handed over, not settled" state: status = 'claimed', for chat,
-- system notes and heartbeats. The earlier steering reservation (pending with a claim time) meant the
-- same thing, so rows left in it by a release that predates this one are moved to the state that now
-- means it. Only agents on an external harness are touched: on the built-in loop a pending row with a claim
-- time is something else. Their outcome is unknown, which the host's rebuild pass already handles for claimed rows.
UPDATE inbound_messages
   SET status = 'claimed'
 WHERE status = 'pending'
   AND claimed_at IS NOT NULL
   AND kind IN ('chat', 'system_note', 'heartbeat')
   AND agent_id IN (SELECT id FROM agents_meta WHERE harness <> 'meiva');

COMMENT ON COLUMN inbound_messages.claimed_at IS
    'When the claim node grabbed this row (pending -> claimed for chat on the built-in harness, pending -> done for lifecycle kinds), or when an external harness was handed it (pending -> claimed for chat, system_note and heartbeat; it leaves claimed only when the harness reports it in context, or back to pending with this column NULL when it refused or lost it). NULL = never claimed. Pickup latency = claimed_at - created_at.';
