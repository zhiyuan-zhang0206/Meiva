CREATE TABLE IF NOT EXISTS llm_usage_ledger (
    call_id text PRIMARY KEY CHECK (call_id <> ''),
    occurred_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    agent_id bigint,
    machine text NOT NULL,
    cluster text NOT NULL,
    source text,
    billing text NOT NULL CHECK (billing IN ('api', 'subscription')),
    usage_kind text NOT NULL,
    model text NOT NULL,
    requested_model text,
    provider text,
    provider_request_id text,
    calls bigint NOT NULL CHECK (calls > 0),
    complete boolean NOT NULL,
    tokens_in bigint CHECK (tokens_in >= 0),
    tokens_out bigint CHECK (tokens_out >= 0),
    tokens_reasoning bigint CHECK (tokens_reasoning >= 0),
    cache_read bigint CHECK (cache_read >= 0),
    cache_write_5m bigint CHECK (cache_write_5m >= 0),
    cache_write_1h bigint CHECK (cache_write_1h >= 0),
    cache_write_30m bigint CHECK (cache_write_30m >= 0),
    cost_usd numeric CHECK (cost_usd >= 0 AND cost_usd < 'Infinity'::numeric),
    price_snapshot jsonb NOT NULL,
    price_source text CHECK (price_source IN ('live', 'legacy', 'historical_catalog')),
    CHECK ((cost_usd IS NULL) = (price_source IS NULL))
);

CREATE INDEX IF NOT EXISTS llm_usage_ledger_occurred_at ON llm_usage_ledger (occurred_at);
CREATE INDEX IF NOT EXISTS llm_usage_ledger_agent_occurred_at ON llm_usage_ledger (agent_id, occurred_at);

COMMENT ON TABLE llm_usage_ledger IS
    'One usage report per source identity. API and subscription sums are disjoint. Rows have no retention expiry.';
COMMENT ON COLUMN llm_usage_ledger.complete IS
    'Whether the usage source considers its reported token counts complete.';
COMMENT ON COLUMN llm_usage_ledger.cost_usd IS
    'Usage-time USD list price, virtual for subscription calls. NULL means no known price.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT, INSERT ON llm_usage_ledger TO meiva_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_gateway') THEN
        REVOKE UPDATE, DELETE ON llm_usage_ledger FROM meiva_gateway;
    END IF;
END
$$;
