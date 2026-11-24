# Events Maintenance — ledger repricing, checkpoint maintenance + alert maintenance

## What it is
Gateway-owned background daemon (`services/upkeep/events_maintenance/daemon.py`). Telemetry and log events are not stored in Postgres (Loki keeps them for seven days), so there is nothing here to replay or derive from them: every `MEIVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` (default 1h) the daemon runs the maintenance steps below. The durable token + cost ledger is `llm_usage_ledger`, written by the LLM call path; this daemon no longer derives usage tables. Checkpoint trim opt-in retired 2026-09-30; its implementation remains unscheduled.

**Role home**: gateway side (pure agent-runner does not run) — `services/supervision/roster/spec.py`'s `ServiceSpec.capabilities=GATEWAY`. The daemon ALWAYS runs on the gateway, and its responsibilities are unconditional: the **ledger repricing**, the **checkpoint size sample**, and the **blob vacuum**. VACUUM reclaims physical space without deleting live checkpoint history.

## Core responsibilities
- **Retired derivations** (2026-11-24): the hourly day-grain rollup, the fold into the token totals and the old hourly unpriced backfill no longer exist, and their tables are dropped; the JSONL mirror replay into `telemetry_events` and the observed-metrics projection are retired with the stage-4 stop of those writes; usage readers use `llm_usage_ledger`, and a ledger row that had no price is priced by the step below.
- **Usage-ledger repricing** (`services/upkeep/events_maintenance/ledger_repricing.py:reprice_unpriced_ledger`, last in the pass): every `llm_usage_ledger` row with `cost_usd IS NULL` is quoted against the archive at its own `occurred_at` and, when the catalog now has the rates, gets its cost, rate snapshot and `price_source = 'historical_catalog'`. A call is written unpriced when the catalog could not price it at the time (`llm_usage_unpriced` alerts on that); a release that declares the rate is all it takes for the next hourly pass to fill the backlog. It is idempotent and never touches a priced row: the select, the `UPDATE ... WHERE cost_usd IS NULL`, and the table's `llm_usage_ledger_price_only` trigger (the gateway holds `UPDATE` on those three columns only) each enforce it. A row the catalog still cannot price stays out of the plan and is retried by the next pass. The pass reads the reviewed archive, which provider plugin prices must equal for the models the repository ships.
- **Unnamed-writer audit count** (`audit_identity.py`, same pass): audit rows whose writer could not name its machine — [Events Maintenance — unnamed-writer audit count](audit-identity.md).
- **Checkpoint physical-size monitoring** (`services/upkeep/events_maintenance/blob_vacuum.py`) — each actual plain-vacuum pass measures `checkpoint_blobs`, `checkpoints`, and `checkpoint_writes` with `pg_total_relation_size` and emits the latest values through the existing OTLP metric path as absolute gauges. Grafana warns at 2.5 GiB and errors at 4 GiB for `checkpoint_blobs`; measurements refresh only in the 05:00-08:00 window or on a force run, so an alert lasts through the daily sampling gap. The alerts prompt an operator-led repack/capacity decision before the disk watermark; they do not change vacuum behavior.
- **Configuration**: `services/upkeep/events_maintenance/daemon.py:events_maintenance_config` reads the entry ConfigBoot once; it builds the frozen `EventsMaintenanceConfig` (`config.py`) and passes it to the loops and passes.
- **Per-loop progress health**: each resident loop owns a separate progress tracker with a hard deadline; a timed-out worker wedges its tracker and makes the aggregate `/healthz` return 503 while sibling loops stay healthy — [Events Maintenance — per-loop progress health](progress-health.md).

## Key dependencies
- [Database Layer](../../../../agent/db/docs/db.md) — the frozen `events` archive was dropped; see the task #1281/#1823 cleanup

## Entry points
- `services/upkeep/events_maintenance/daemon.py` — `.venv/bin/python -m services.upkeep.events_maintenance.daemon`
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`services/supervision/roster/healthz.py`)

## Process composition

Entry capture, the gates each entry binds, and the composition inputs the daemon's slice builder requires: [Events Maintenance — process composition](process-composition.md).
