# OTLP Export Backpressure

## Contract

OTLP failure cannot block or break the event drain after its JSONL mirror write,
and a collector outage cannot add an unbounded wait to an agent turn. Loss is an
explicit bounded-degradation policy: producers count and report shedding while
the collector sidecar owns durable mirroring after acceptance.

## Signal paths

- `base/telemetry/otlp/telemetry_otlp.py` places events into a bounded 2048-entry queue with
  `put_nowait`; a full queue increments the cumulative drop counter and reports
  the loss outside the saturated log lane. In-memory metric recording never waits on the
  exporter queue.
- SDK-owned log and metric threads perform network I/O. Each OTLP/HTTP exporter
  has the standard 10-second request and retry budget. This gives a restarting
  local receiver time to accept a retried batch. Export runs outside the producer
  thread and the caller's flush and shutdown budgets stay bounded.
- `base/telemetry/tracing.py:OtlpJsonHttpSpanExporter` gives each protobuf trace batch one
  2-second POST. Three consecutive failed batches open a 30-second circuit.
  During cooldown it returns `FAILURE` without encoding or posting and counts
  dropped batches and spans; after cooldown exactly one half-open probe runs.
  Probe success closes the circuit and resets consecutive failures, while probe
  failure starts a fresh cooldown.
- Flush uses a distinct FIFO receipt and one finite caller deadline, including
  marker admission. The sole worker requests a 500 ms force-flush budget from
  each SDK provider. Shutdown closes admission and joins the same worker
  finitely; the worker requests a 2-second provider flush then SDK shutdown.
  SDK calls may outlive the caller deadline and remain owned. The HTTP exporters
  retain their own network budget. These observations do not promise
  delivery or native termination. See [OTLP exit flush](exit-flush.md).

## Recovery

The initial collector reachability probe is bounded at 1.5 seconds. A missing
sidecar disables exporter construction for five minutes, reports the episode,
and retries on the backend's next initialization opportunity; trace setup uses
one daemon retry loop on the same cadence. Successful trace recovery reports the
preceding consecutive failures and cumulative circuit drops.

Parent node: [OTLP export backend and trace ship](telemetry-otlp.md).

## Queue loss

The emitter, OTLP log queue, and OTel SDK batch queue report actual loss as `event_log_drop`, with
`n`, `queue`, and `last_dropped_at`. Local error diagnostics bypass the saturated
queue and always write stderr, including standalone scripts before logging setup.
Delayed summaries preserve the actual loss time. OTLP loss reports go straight to the JSONL mirror and metric instruments;
`meiva_event_log_drop_last_dropped_at_ratio` drives the immediate error-level
`meiva-ops-telemetry-queue-loss` Grafana alert, resolving after five loss-free minutes.
The metrics lane can report even while log delivery is saturated. A collector or
metrics-export outage still delays remote visibility; existing exporter-silence
alerts cover that failure. These are observable losses, not a durable delivery
guarantee or a reconstruction of missing calls.

The OTel SDK observer handles its pinned queue-full warning before OTel's
duplicate-log filter, records each loss, and suppresses the original warning
after replacing it with an error outside the emitter. A real bounded SDK queue
test locks this upstream diagnostic contract.

## SDK export loss

The log processor and HTTP log exporter send SDK internal metrics through the
existing metrics provider. Provider construction defaults the SDK setting
`OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED` to `true`. The log-exporter's local
diagnostics stay available in stderr and process logs even during an outage.

- `otel.sdk.processor.log.processed` with `error.type=queue_full` counts logs
  evicted by the SDK batch processor.
- `otel.sdk.exporter.log.exported` with `error.type` counts logs in failed
  exports that raised a transport error. For SDK 1.44.0, HTTP response failures
  are visible on `otel.sdk.exporter.operation.duration` via
  `http.response.status_code`, at batch granularity.
- These metrics travel through the collector. After an interruption a surviving
  process can export the cumulative counts on a later metrics pass. The local
  diagnostics cover periods when a process exits before its metrics can reach
  the collector.
