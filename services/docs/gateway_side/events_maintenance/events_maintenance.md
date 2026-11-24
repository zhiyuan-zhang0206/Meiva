# Events Maintenance — mirror replay, checkpoint maintenance + alert maintenance

## What it is
Gateway-owned background daemon (`services/upkeep/events_maintenance/daemon.py`). The telemetry and log record lives in `telemetry_events` (Postgres, permanent); this daemon keeps the derived tables and the record itself in step with it: every `MEIVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` (default 1h) it replays this machine's JSONL mirror into `telemetry_events`. The durable token + cost ledger is `llm_usage_ledger`, written by the LLM call path; this daemon no longer derives usage tables. Checkpoint trim opt-in retired 2026-09-30; its implementation remains unscheduled.

**Role home**: gateway side (pure agent-runner does not run) — `services/supervision/roster/spec.py`'s `ServiceSpec.capabilities=GATEWAY`. The daemon ALWAYS runs on the gateway, and its responsibilities are unconditional: the **mirror replay**, the **checkpoint size sample**, and the **blob vacuum**. VACUUM reclaims physical space without deleting live checkpoint history.

## Core responsibilities
- **Retired derivations** (2026-11-24): the hourly day-grain rollup into `agent_model_tokens_daily` / `agent_metrics_daily`, the fold into `agent_model_tokens_total`, and the hourly unpriced backfill no longer run; usage readers use `llm_usage_ledger`. `token_totals.py`, `unpriced_backfill.py`, the table definitions and `meiva metrics backfill-unpriced` stay for the unpriced-pricing replacement to delete; the tables are dropped by a later migration ([plan](../../../../future/infra/data/drop-agent-model-tokens-tables.md)).
- **Unnamed-writer audit count** (`audit_identity.py`, same pass): audit rows whose writer could not name its machine — [Events Maintenance — unnamed-writer audit count](audit-identity.md).
- **Observed Inspector metrics**: persisted typed live projection — [Observed Inspector Metrics](observed-metrics.md).
- **Telemetry events replay**: the hourly mirror → `telemetry_events` pass — [Telemetry Events Replay](telemetry-replay.md).
- **Checkpoint physical-size monitoring** (`services/upkeep/events_maintenance/blob_vacuum.py`) — each actual plain-vacuum pass measures `checkpoint_blobs`, `checkpoints`, and `checkpoint_writes` with `pg_total_relation_size` and emits the latest values through the existing OTLP metric path as absolute gauges. Grafana warns at 2.5 GiB and errors at 4 GiB for `checkpoint_blobs`; measurements refresh only in the 05:00-08:00 window or on a force run, so an alert lasts through the daily sampling gap. The alerts prompt an operator-led repack/capacity decision before the disk watermark; they do not change vacuum behavior.
- **Configuration**: `services/upkeep/events_maintenance/daemon.py:events_maintenance_config` reads the entry ConfigBoot once; it builds the frozen `EventsMaintenanceConfig` (`config.py`, with the mirror directory) and passes it to the loops and passes.
- **Per-loop progress health**: each resident loop owns a separate progress tracker with a hard deadline; a timed-out worker wedges its tracker and makes the aggregate `/healthz` return 503 while sibling loops stay healthy — [Events Maintenance — per-loop progress health](progress-health.md).

## Key dependencies
- [Database Layer](../../../../agent/db/docs/db.md) — the frozen `events` archive was dropped; see the task #1281/#1823 cleanup
- `telemetry_events` — the record the mirror replay restores

## Entry points
- `services/upkeep/events_maintenance/daemon.py` — `.venv/bin/python -m services.upkeep.events_maintenance.daemon`
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`services/supervision/roster/healthz.py`)

## Process composition

Entry capture, the gates each entry binds, and the composition inputs the daemon's slice builder requires: [Events Maintenance — process composition](process-composition.md).
