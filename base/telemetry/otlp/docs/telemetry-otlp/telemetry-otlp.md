# OTLP export backend

## What it is

`base/telemetry/otlp/telemetry_otlp.py` is the agent-side write half of the OTel + Tempo +
Loki + Prometheus + Grafana stack (2026-08-11 decision). The local OTel
Collector sidecar (`meiva-otel-collector`, task #1266) delivers the three signals.
Which homes may export
or collect, and the `cluster` Resource boundary that isolates co-located homes,
are specified in [Telemetry cluster isolation](cluster-isolation.md).

- `base/telemetry/otlp/telemetry_otlp.py` — events → OTLP logs + metrics when enabled
  (default **on** for the registered `.meiva` production identity). Production
  exec children hold their records until exit, hold saturation, or the max-age
  bound (`base.telemetry.otlp.telemetry_otlp_defer`, task #3816 M4b) instead of warming the
  exporter stack at boot. The child's entry builds the `ChildDeferPlan` they follow from its
  environment (`base.telemetry.delivery.defer_plan`); the exporter never reads it. Test/ad-hoc
  exec children stay off unless an operator supplies an explicit endpoint. Their request-file handshake is not an export
  authority.
- `base/telemetry/tracing.py` — spans → OTLP/HTTP (protobuf, content-stripped) to
  the sidecar's `/v1/traces`, which relays to Tempo. Producer timeout, circuit-breaker, shedding, and synchronous-flush
  bounds live in [OTLP export backpressure](export-backpressure.md).
- The sidecar also **scrapes** the traditional SRE layer — host, Postgres,
  Redis — into the same metrics fan-out:
  [Infrastructure metrics](infra-metrics/infra-metrics.md).

## Core responsibilities

- **Logs** (`_emit_log`) — every `Event` becomes one OTLP LogRecord (Loki).
  Body = the full event as JSON in the mirror shape; event_name / category /
  level / machine / cluster / process / source (+ agent_id / target_agent_id
  when set) as attributes; trace_id / span_id ride the LogRecord fields, so Loki
  rows correlate with Tempo spans.
  Severity mapping: debug 5 / info 9 / warning 13 / error 17 / critical 21.
- **Metrics** (`_record_metrics`) — declared telemetry fields produce
  Prometheus series through the [explicit OTLP metrics mapping](metrics-mapping.md).
  The source collector adds the roster machine label.
- **Traces** — exported with the cluster Resource to the sidecar's `/v1/traces`
  (OTLP/HTTP protobuf, content-stripped before leaving the process); the sidecar
  relays them to Tempo (gateway/single-box → `{MEIVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces`;
  pure runner → the gateway collector's authenticated ingress). Traces are
  telemetry: lossy by design, with no local copy and no replay
  ([delivery topology](delivery-topology.md)). `MEIVA_TELEMETRY_OTLP_ENABLED`
  off stops the whole OTLP surface, recording included.
- **Failure isolation** — the producer queue, network deadlines, trace circuit,
  drop accounting, initialization retries, and exec-child flush ceilings are
  specified in [OTLP export backpressure](export-backpressure.md).

## Read side

Enablement flags, endpoint defaults and the read topology have their own node:
[OTLP flag semantics & endpoints](flag-semantics.md).

## Key dependencies

- `base/telemetry/pipeline_build.py` — caller: the pipeline's writer runs the OTLP export (its only sink); the concrete pipeline closes its own exporter after
  joining the ordinary writer. `_drain_on_exit` observes that same pipeline.
- OTel SDK (`opentelemetry-*`) — `telemetry_otlp_sdk` (providers, views, resource) is imported
  lazily at worker bring-up, so flag-off processes never pay for the SDK; the backend modules
  import only the OTel API value types (`LogRecord`, `Observation`, `Context`).
- `base/config/domains/observability/settings.py` — the producer-local `telemetry_otlp_*`
  settings plus gateway-local backend URLs.
- `base/deploy/release/collector/artifact.py` — pinned downloader for release prep and
  `cli/commands/observability/otel_collector.py` converge. Converge and
  `deploy/otel-collector/otel-collector.yaml` implement cluster filtering and
  the credential-free (peer, monitoring role) Postgres receiver. Root health: `services/supervision/roster/spec.py`
  (`meiva-otel-collector`) + `services/supervision/healthchecks/otel_collector.py`.

## Entry points

- `base/telemetry/otlp/telemetry_otlp.py:OtlpBackend` — built by the root
  with its explicit eligibility callback, mirror directory and cluster home (the home its metrics
  resource labels the cluster from) and owned by `ExportingEventPipeline`;
  no-op when the flag is off; `flush()` / `shutdown()` observe that owner. `telemetry_otlp_worker.OtlpWorker`
  owns the one writer and each partial SDK resource through teardown.
- `base/telemetry/otlp/telemetry_otlp_exporting.py:ExportingEventPipeline` — one ordinary
  writer beside the exporter it owns; every stop stage receives only the deadline's
  real remaining time.
- `base/telemetry/pipeline_build.py:build_pipeline` — constructs the concrete writer
  and hands its backend export to the pipeline's writer.

## Notes

- Routine OTLP diagnostics use loguru with `_no_emitter`, reaching stderr/file
  without re-entering the pipeline. Registered disabled/recovered events also
  record the otherwise invisible episode in the event stream (when the collector is reachable).
- Runner exporter component IDs stay `otlphttp/tempo`, `otlphttp/loki` and
  `otlphttp/prometheus` across the direct-backend → relay cutover so their
  collector self-metrics keep their series. Every exporter keeps a bounded
  in-memory sending queue; a collector restart drops what was queued.
  Queue/drop/silence observability lives in
  [Infrastructure metrics](infra-metrics/infra-metrics.md).
- Exit flush (task #4320) — both providers build with
  `shutdown_on_exit=False`; the single ordered exit seam and its accepted
  semantics: [OTLP exit flush](exit-flush.md).
- Collector/Grafana operations:
  `docs/conventions/runbook.md` (Observability / Tracing).
- Parent node: [Base Library](../../../../docs/base.md).
