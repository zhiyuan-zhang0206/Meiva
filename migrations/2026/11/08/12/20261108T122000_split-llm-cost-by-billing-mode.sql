-- Split an agent's LLM spend by the billing mode of the loop that ran each call.
--
-- `llm_usage.source` names that loop: the built-in `meiva` runtime (and every non-harness caller —
-- compaction, web.fetch, understand, decisions, batch, embeddings) is metered, so its price is
-- money owed; an external harness (claude_code, codex, antigravity) is covered by a subscription,
-- so its usage-time price is list value consumed, not a charge. The Inspector panel shows the two
-- as separate blocks and hides a mode the agent never ran.
--
-- `cost_usd_subscription` and `unpriced_calls_subscription` are additive parts of the totals the
-- same rows already carry: cost_usd_api = cost_usd - cost_usd_subscription, and likewise for the
-- unpriced counts. Rows written before this migration default to 0, which reads as "all API" —
-- true for every day that predates external harnesses, and the only honest answer for a projection
-- that never recorded the source.
ALTER TABLE agent_metric_observations
    ADD COLUMN IF NOT EXISTS cost_usd_subscription NUMERIC NOT NULL DEFAULT 0
        CHECK (cost_usd_subscription >= 0 AND cost_usd_subscription <> 'NaN'::numeric),
    ADD COLUMN IF NOT EXISTS unpriced_calls_subscription BIGINT NOT NULL DEFAULT 0
        CHECK (unpriced_calls_subscription >= 0);

ALTER TABLE agent_metric_days
    ADD COLUMN IF NOT EXISTS cost_usd_subscription NUMERIC NOT NULL DEFAULT 0
        CHECK (cost_usd_subscription >= 0 AND cost_usd_subscription <> 'NaN'::numeric),
    ADD COLUMN IF NOT EXISTS unpriced_calls_subscription BIGINT NOT NULL DEFAULT 0
        CHECK (unpriced_calls_subscription >= 0);

ALTER TABLE agent_model_tokens_daily
    ADD COLUMN IF NOT EXISTS cost_usd_subscription DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unpriced_calls_subscription BIGINT NOT NULL DEFAULT 0;
