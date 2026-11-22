# Events Maintenance — cost-ledger rollup + alert maintenance

## What it is
Gateway-owned background daemon (`services/upkeep/events_maintenance/daemon.py`). The telemetry and log record lives in `telemetry_events` (Postgres, permanent); this daemon keeps the derived tables and the record itself in step with it: every `MEIVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` (default 1h) it replays this machine's JSONL mirror into `telemetry_events`, and recomputes the last eight closed UTC days into `agent_metrics_daily` and `agent_model_tokens_daily`, the cluster's **durable token + cost ledger**. Checkpoint trim opt-in retired 2026-09-30; its implementation remains unscheduled.

**Role home**: gateway side (pure agent-runner does not run) — `services/supervision/roster/spec.py`'s `ServiceSpec.capabilities=GATEWAY`. The daemon ALWAYS runs on the gateway, and its responsibilities are unconditional: the **cost-ledger rollup**, the **mirror replay**, the **checkpoint size sample**, and the **blob vacuum**. VACUUM reclaims physical space without deleting live checkpoint history.

## Core responsibilities
- **Day-grain rollup** (`services/upkeep/events_maintenance/rollup.py:compute_rollup`, unconditional): each pass recomputes the last eight closed UTC days (`RECOMPUTE_DAYS`, covering the mirror replay's seven-day lateness) from `telemetry_events` into `agent_model_tokens_daily` (per agent × day × model: calls, token sums, usage-time cost split by billing mode (`llm_usage.source`), costed/unpriced calls; `estimated_calls` is never written; recorded usage-time prices are never repriced, while blank `cost_usd IS NULL` rows are normalized and priced at `occurred_at` when archive pricing exists) and `agent_metrics_daily` (per agent × UTC-day turn/exec counts, exact turn-duration sum/min/max, and a mergeable `floor(duration_seconds)` histogram for p50/p90). A monotone guard (`ON CONFLICT … WHERE` the recompute has at least as many calls, turns and execs) means a day that `telemetry_events` holds only in part never lowers a ledger row. The operator CLI `python -m services.upkeep.events_maintenance.rollup --from YYYYMMDD --to YYYYMMDD` recomputes a range after a backfill.
- **Unpriced historical backfill** (`services/upkeep/events_maintenance/unpriced_backfill.py:run_unpriced_backfill`, right after the rollup): idempotently scans blank `cost_usd IS NULL` (`unpriced_calls > 0`) rows in `agent_metric_observations` and historical `agent_model_tokens_daily` days, normalizes effort/`[1m]` suffixes, quotes strictly at `occurred_at` against `pricing_catalog_archive.json`, recomputes affected `(agent_id, day)` rows in `agent_metric_days`, updates historical ledger days, and rebuilds `agent_model_tokens_total` if any updated day falls at or before the fold watermark. Already-priced rows are never modified and `telemetry_events` remains append-only. Operators can also inspect or trigger this pass explicitly via `meiva metrics backfill-unpriced --dry-run` / `--apply`.
- **Token totals** (`token_totals.py`, right after the rollup): settled ledger days fold into `agent_model_tokens_total` — [Agent Token Totals](token-totals.md).
- **Unnamed-writer audit count** (`audit_identity.py`, same pass): audit rows whose writer could not name its machine — [Events Maintenance — unnamed-writer audit count](audit-identity.md).
- **Observed Inspector metrics**: persisted typed live projection — [Observed Inspector Metrics](observed-metrics.md).
- **Telemetry events replay**: the hourly mirror → `telemetry_events` pass — [Telemetry Events Replay](telemetry-replay.md).
- **Checkpoint physical-size monitoring** (`services/upkeep/events_maintenance/blob_vacuum.py`) — each actual plain-vacuum pass measures `checkpoint_blobs`, `checkpoints`, and `checkpoint_writes` with `pg_total_relation_size` and emits the latest values through the existing OTLP metric path as absolute gauges. Grafana warns at 2.5 GiB and errors at 4 GiB for `checkpoint_blobs`; measurements refresh only in the 05:00-08:00 window or on a force run, so an alert lasts through the daily sampling gap. The alerts prompt an operator-led repack/capacity decision before the disk watermark; they do not change vacuum behavior.
- **Configuration**: `services/upkeep/events_maintenance/daemon.py:events_maintenance_config` reads the entry ConfigBoot once; it builds the frozen `EventsMaintenanceConfig` (`config.py`, with the mirror directory) and passes it to the loops and passes.
- **Per-loop progress health**: each resident loop owns a separate progress tracker with a hard deadline; a timed-out worker wedges its tracker and makes the aggregate `/healthz` return 503 while sibling loops stay healthy — [Events Maintenance — per-loop progress health](progress-health.md).

## Key dependencies
- [Database Layer](../../../../agent/db/docs/db.md) — writes the two rollup tables (the frozen `events` archive was dropped; see the task #1281/#1823 cleanup)
- `telemetry_events` — the source of the rollup and observation recovery

## Entry points
- `services/upkeep/events_maintenance/daemon.py` — `.venv/bin/python -m services.upkeep.events_maintenance.daemon`
- `services/upkeep/events_maintenance/rollup.py` — also the operator CLI for a range of days
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`services/supervision/roster/healthz.py`)

## Process composition

Entry capture, the gates each entry binds, and the composition inputs the daemon's slice builder requires: [Events Maintenance — process composition](process-composition.md).
