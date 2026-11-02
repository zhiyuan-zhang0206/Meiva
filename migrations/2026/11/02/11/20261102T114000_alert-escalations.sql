-- The escalation ledger for alerts that reached a person (or, in shadow mode, would have).
-- Idempotent: a fresh database builds this from db/schema.sql first and then replays this file.
CREATE TABLE IF NOT EXISTS alert_escalations (
    id                    BIGSERIAL PRIMARY KEY,
    source_kind           TEXT NOT NULL CHECK (source_kind IN ('alerts', 'signal_state')),
    source_id             BIGINT NOT NULL,
    escalation_count      INTEGER NOT NULL DEFAULT 0 CHECK (escalation_count >= 0),
    shadow_escalations    INTEGER NOT NULL DEFAULT 0 CHECK (shadow_escalations >= 0),
    last_delivery_at      TIMESTAMPTZ,
    last_delivery_result  TEXT CHECK (last_delivery_result IN ('shadow', 'sent', 'held', 'failed')),
    cooldown_until        TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS alert_escalations_source_idx
    ON alert_escalations (source_kind, source_id);

COMMENT ON TABLE alert_escalations IS
    'The escalation ledger: who was told about a condition, when, and how it went. Condition facts are read from the source row every pass, never copied here.';

COMMENT ON COLUMN alert_escalations.source_kind IS
    'Which condition table source_id refers to: alerts (Grafana-sourced episodes) or signal_state (detector-sourced episodes).';

COMMENT ON COLUMN alert_escalations.escalation_count IS
    'Escalations delivered to a person. Only the escalation loop writes it, and only when a delivery was accepted.';

COMMENT ON COLUMN alert_escalations.shadow_escalations IS
    'Would-be escalations recorded while the mode is shadow: the loop decides and logs but sends nothing, so the volume is measurable before the gate opens.';

COMMENT ON COLUMN alert_escalations.cooldown_until IS
    'No escalation before this instant. The rate limit is stored here rather than in memory so a restart cannot reset it.';
