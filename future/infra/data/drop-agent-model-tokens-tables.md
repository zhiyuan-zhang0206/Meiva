# Drop the retired agent token day tables

Usage readers and the hourly rollup now use `llm_usage_ledger`, so
`agent_model_tokens_daily`, `agent_model_tokens_total` and
`agent_model_tokens_total_through` have no reader and no scheduled writer. Keep the
tables through the supported rollback window: the previous events-maintenance daemon
still rolls and folds them, and the previous usage readers still read them.

The tables hold the only copy of usage from before the ledger began (the first per-call report,
2026-09-26). That history is imported into the ledger as daily reports, and the tables are
archived with `pg_dump` (compressed, kept on the observability station) before the drop:

1. `meiva metrics import-legacy-usage --cluster <label>` (dry run) reports the earliest and latest
   day, rows, per-model calls, tokens and cost, the days from the cutoff on where the day table and
   the ledger overlap, the folded-totals check, and the rows whose billing cannot be derived.
2. `--apply` imports one report per (day, agent, model) with `call_id`
   `legacy-daily:<day>:<agent>:<model>` and `usage_kind` `legacy_daily`, keeping the stored cost;
   it refuses while a `mixed`-billing row exists. Re-running it skips what is imported.
3. Compare the lifetime views (fleet graph lifetime tokens, `meiva metrics agents --days 30`,
   `agent_usage.py --lifetime`) before and after, then archive the tables.

Drop them when all of the following hold:

1. The release that removed the rollup, the fold and the usage readers is deployed on
   the gateway and every runner, and its verification window is over (no statement
   reads or writes the three tables since the rollout).
2. The code that named the tables is gone (`token_totals.py`, `unpriced_backfill*.py` and
   the `meiva metrics backfill-unpriced` command left with the usage-ledger repricing
   change); grep for the three table names to confirm nothing else remains. The legacy import
   command is removed once the import is verified.
3. The history is imported and verified, and the archive exists.
4. The supported rollback target no longer includes the old rollup.

Then add a timestamped migration that drops the three tables (the total-through
watermark first), synchronize `db/schema.sql` with its comments and indexes, remove
the tables from the provisioning fixture (`tests/fixtures/provisioning.py`) and
verify both a fresh schema and an upgrade. The creation migrations stay immutable.
`agent_metrics_daily` and the `agent_metric_*` tables belong to the later telemetry
write-stop and contract stages, not to this drop.
