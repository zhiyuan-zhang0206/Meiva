-- OpenAI reports cache writes in one class, the 30-minute one. For part of 2026-10-29 the usage
-- mapping filed OpenAI's creation counts as 5-minute writes, a class the catalog never declared for
-- the model, so those calls were recorded with no cost. The first statements move those counts to
-- the class the vendor reports (the same rule as `settle_writes` in base/lm/pricing/cache_writes.py);
-- the maintenance pass then prices the rows at their own time.
-- The trigger is created after that repair: from here on a ledger row changes in exactly one way,
-- from unpriced to priced, and the gateway may write only the three price columns.
DROP TRIGGER IF EXISTS llm_usage_ledger_price_only ON llm_usage_ledger;

UPDATE llm_usage_ledger
SET cache_write_30m = NULLIF(
        COALESCE(cache_write_5m, 0) + COALESCE(cache_write_1h, 0) + COALESCE(cache_write_30m, 0), 0),
    cache_write_5m = NULL,
    cache_write_1h = NULL
WHERE (provider = 'openai' OR (provider IS NULL AND model LIKE 'gpt-%'))
  AND cost_usd IS NULL
  AND (COALESCE(cache_write_5m, 0) > 0 OR COALESCE(cache_write_1h, 0) > 0);

CREATE INDEX IF NOT EXISTS llm_usage_ledger_unpriced
    ON llm_usage_ledger (occurred_at, call_id) WHERE cost_usd IS NULL;

CREATE OR REPLACE FUNCTION llm_usage_ledger_price_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.cost_usd IS NOT NULL
       OR NEW.cost_usd IS NULL
       OR (to_jsonb(NEW) - 'cost_usd' - 'price_snapshot' - 'price_source')
          IS DISTINCT FROM (to_jsonb(OLD) - 'cost_usd' - 'price_snapshot' - 'price_source') THEN
        RAISE EXCEPTION 'llm_usage_ledger: a row changes only from unpriced to priced (call %)', OLD.call_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER llm_usage_ledger_price_only
    BEFORE UPDATE ON llm_usage_ledger
    FOR EACH ROW EXECUTE FUNCTION llm_usage_ledger_price_only();

COMMENT ON INDEX llm_usage_ledger_unpriced IS
    'The rows the events-maintenance pass still has to price; empty when every row has a cost.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_gateway') THEN
        GRANT UPDATE (cost_usd, price_snapshot, price_source) ON llm_usage_ledger TO meiva_gateway;
    END IF;
END
$$;
