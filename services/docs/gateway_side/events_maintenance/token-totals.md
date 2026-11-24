# Agent Model Tokens Total

`services/upkeep/events_maintenance/token_totals.py` folds settled days of
`agent_model_tokens_daily` into `agent_model_tokens_total` (every ledger column, per agent and
model) and moves the watermark in `agent_model_tokens_total_through`. A fold is one transaction under
an advisory lock and is idempotent.

Nothing schedules it any more. The hourly pass stopped rolling and folding on 2026-11-24, and every
usage reader (the fleet graph, the Inspector, `meiva stats`, `meiva metrics`, and the agent usage
script) reads `llm_usage_ledger`. Only the unpriced backfill still calls into this module; both are
deleted with it, and the three tables are dropped by a later migration
([plan](../../../../future/infra/data/drop-agent-model-tokens-tables.md)).
