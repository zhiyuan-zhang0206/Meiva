# Drop the unwritten telemetry tables (stage 5)

Stage 4 stopped every writer of `telemetry_events` and of the observation projection
(`agent_metric_observations`, `agent_metric_days`) and deleted the JSONL mirror and its replay
(`agent_metric_file_cursors`). The tables are still in the schema and still granted. This note is
what is left; it is the contract step and it is destructive, so it ships as its own change after
the stage-4 release has run and every process on the cluster has restarted onto it.

Before the migration:

- Confirm no process runs a release older than stage 4: `meiva cluster status` shows one version per
  machine. A stage-3 process still inserts into `telemetry_events`; dropping the table under it makes
  its drain thread fail on every batch.
- Archive the rows from 2026-10-02 on (`pg_dump` of `telemetry_events`, compressed, kept on the
  observability station) unless the owner of the redesign says to skip the archive. `llm_usage` rows
  already live in `llm_usage_ledger`; the rest is lossy operational data by decision.

The migration, as one timestamped SQL file under `migrations/` (an existing migration is never
edited), with `db/schema.sql` and the baseline kept in step:

- `DROP` `telemetry_events` with its partitions, `ensure_telemetry_event_partitions`, the triggers
  and the view `telemetry_events_attributed`; `DROP` `telemetry_owner_inference`.
- `DROP` `agent_metric_observations`, `agent_metric_days`, `agent_metric_collection` and
  `agent_metric_file_cursors`, and the view that joins `agent_metric_collection`.
- `ALTER TABLE signal_state DROP COLUMN first_event_id, DROP COLUMN last_event_id`: the evidence
  pointers name Loki stream ids that expire after seven days.

With it:

- Remove the table names from `base/cluster/authority/groups.py` (the gateway append-only tuple and
  the runner insert and update grants), from `tests/fixtures/provisioning.py`, from
  `meizel.policy.toml` if any entry remains, and from the migration probes in
  `scripts/ci/test_migrations_apply.sh` and `scripts/audit/tests/test_schema_drift.py`.
- Verify an upgrade from the previous release's schema and a fresh provision, as the migration guide
  requires, and run `scripts/audit/schema_drift.py` against the result.

[Decision](../../../docs/decisions/observability/telemetry/2026-11-24-telemetry-stops-writing-postgres.md)
