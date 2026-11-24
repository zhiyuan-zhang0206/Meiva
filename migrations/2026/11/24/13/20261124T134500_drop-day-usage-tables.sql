-- The usage day tables and the turn/exec day counters have no reader and no writer: usage lives in
-- llm_usage_ledger (the pre-ledger days were imported as legacy_daily reports), and the Inspector no
-- longer serves turn and exec statistics. Operators archive the tables with pg_dump before this runs.
DROP TABLE IF EXISTS agent_model_tokens_total_through RESTRICT;
DROP TABLE IF EXISTS agent_model_tokens_total RESTRICT;
DROP TABLE IF EXISTS agent_model_tokens_daily RESTRICT;
DROP TABLE IF EXISTS agent_metrics_daily RESTRICT;
