-- Attribution of historical telemetry rows whose emitter dropped agent_id.
--
-- The emitter stopped passing agent_id at the emit site on 2026-10-09 (fixed for
-- turn_end on 2026-10-16, aa4eaa9c5; the exec/code family is still open, task
-- #6921). Those rows cannot be attributed from their own payload.
--
-- `telemetry_events` is append-only: UPDATE, DELETE and TRUNCATE are rejected by
-- trigger for every role, including the owner (docs/decisions/data/database/
-- 2026-10-02-telemetry-events-in-postgres.md). So the repair is a *read* face,
-- not a rewrite:
--
--   telemetry_owner_inference  one row per trace whose llm_usage rows name
--                              exactly one agent — the only evidence available
--                              today, seeded once (the seed is a data step, not
--                              part of this migration; see the decision note).
--   telemetry_events_attributed  every column of telemetry_events, with
--                              agent_id = coalesce(row, inferred) plus
--                              agent_id_source saying which one a reader got:
--                              observed | inferred | unattributed.
--
-- Nothing here touches an existing row.

-- Idempotent on purpose: the objects were created by hand on the live cluster on
-- 2026-10-18 before this migration merged, and that stamp was removed again (it
-- made every image schema-behind). Re-applying must therefore be a no-op there.
CREATE TABLE IF NOT EXISTS telemetry_owner_inference (
    trace_id   TEXT PRIMARY KEY,
    agent_id   BIGINT NOT NULL,
    evidence   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE telemetry_owner_inference IS
    'Trace to agent evidence for rows whose own agent_id is NULL. Derived, append-mostly, and never a substitute for the row''s own agent_id; read through telemetry_events_attributed.';

CREATE OR REPLACE VIEW telemetry_events_attributed AS
SELECT e.id,
       e.event_uid,
       e.ts,
       e.recorded_at,
       e.trace_id,
       e.span_id,
       coalesce(e.agent_id, m.agent_id) AS agent_id,
       CASE
           WHEN e.agent_id IS NOT NULL THEN 'observed'
           WHEN m.agent_id IS NOT NULL THEN 'inferred'
           ELSE 'unattributed'
       END AS agent_id_source,
       e.machine,
       e.cluster,
       e.process,
       e.category,
       e.event_name,
       e.level,
       e.source,
       e.target_agent_id,
       e.attributes,
       e.imported_from
FROM telemetry_events e
LEFT JOIN telemetry_owner_inference m ON m.trace_id = e.trace_id;

COMMENT ON VIEW telemetry_events_attributed IS
    'telemetry_events with agent_id resolved from telemetry_owner_inference when the row has none; agent_id_source separates the observed value from the inferred one (docs/decisions/observability/telemetry/2026-10-18-historical-agent-attribution-via-view.md).';

-- Readers reach both objects through the cluster's group roles. Stated here as
-- well as in db/schema.sql because default privileges only cover objects the
-- owner creates after they are set: an environment whose group roles already
-- existed would otherwise end up with an object nobody can read.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT ON telemetry_owner_inference, telemetry_events_attributed TO meiva_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_gateway') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON telemetry_owner_inference TO meiva_gateway;
        GRANT SELECT ON telemetry_events_attributed TO meiva_gateway;
    END IF;
END $$;
