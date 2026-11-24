# Drop the retired agent token day tables

Usage readers and the hourly rollup now use `llm_usage_ledger`, so
`agent_model_tokens_daily`, `agent_model_tokens_total` and
`agent_model_tokens_total_through` have no reader and no scheduled writer. Keep the
tables through the supported rollback window: the previous events-maintenance daemon
still rolls and folds them, and the previous usage readers still read them.

The tables hold the only copy of usage from before the ledger import began
(2026-09-26). Before the drop, decide whether that history is wanted and, if it is,
import it (as aggregated reports) or archive it with `pg_dump`.

Drop them when all of the following hold:

1. The release that removed the rollup, the fold and the usage readers is deployed on
   the gateway and every runner, and its verification window is over (no statement
   reads or writes the three tables since the rollout).
2. The code that named the tables is gone (`token_totals.py`, `unpriced_backfill*.py` and
   the `meiva metrics backfill-unpriced` command left with the usage-ledger repricing
   change); grep for the three table names to confirm nothing else remains.
3. The supported rollback target no longer includes the old rollup.

Then add a timestamped migration that drops the three tables (the total-through
watermark first), synchronize `db/schema.sql` with its comments and indexes, remove
the tables from the provisioning fixture (`tests/fixtures/provisioning.py`) and
verify both a fresh schema and an upgrade. The creation migrations stay immutable.
`agent_metrics_daily` and the `agent_metric_*` tables belong to the later telemetry
write-stop and contract stages, not to this drop.
