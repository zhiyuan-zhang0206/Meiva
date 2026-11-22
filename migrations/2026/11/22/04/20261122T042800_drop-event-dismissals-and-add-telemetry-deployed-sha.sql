-- Big Bang Alert / Diagnostics cutover (Task #7541):
-- 1. Retire permanent class-level dismissals (`event_dismissals`).
-- 2. Record the emitting process's loaded commit SHA on `telemetry_events` so
--    Diagnostics automatically archives errors from prior releases.

DROP TABLE IF EXISTS event_dismissals;

ALTER TABLE telemetry_events
    ADD COLUMN IF NOT EXISTS deployed_sha TEXT NOT NULL DEFAULT '';
