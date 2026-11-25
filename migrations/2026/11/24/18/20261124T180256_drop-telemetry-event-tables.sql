-- Telemetry and log events are stored in Loki only (docs/decisions/observability/telemetry/
-- 2026-11-24-telemetry-stops-writing-postgres.md): nothing writes or reads telemetry_events, the
-- owner-inference table behind its read view, or the observation projection tables any more, and
-- the evidence pointers of signal_state named ids that exist only in Loki for seven days.
-- Lossy by design; operators archive telemetry_events with pg_dump before this runs.
-- The view first (it reads both tables), then the tables; dropping the partitioned parent drops
-- its monthly partitions, its default partition, its indexes and its append-only triggers.
DROP VIEW IF EXISTS telemetry_events_attributed;
DROP TABLE IF EXISTS telemetry_owner_inference RESTRICT;
DROP TABLE IF EXISTS telemetry_events RESTRICT;
DROP FUNCTION IF EXISTS public.ensure_telemetry_event_partitions(INT, INT);
DROP FUNCTION IF EXISTS reject_telemetry_events_rewrite();
DROP TABLE IF EXISTS agent_metric_observations RESTRICT;
DROP TABLE IF EXISTS agent_metric_days RESTRICT;
DROP TABLE IF EXISTS agent_metric_file_cursors RESTRICT;
DROP TABLE IF EXISTS agent_metric_collection RESTRICT;
ALTER TABLE signal_state DROP COLUMN IF EXISTS first_event_id, DROP COLUMN IF EXISTS last_event_id;
COMMENT ON TABLE signal_state IS
    'One row per open episode of a condition, touched while it persists and closed when it stops. The reports themselves are in the event stream; this table answers whether the condition is still true.';
