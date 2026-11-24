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
- SDK-owned log and metric threads perform network I/O. The OTLP log exporter
  shares a 3.5-second deadline across one SDK batch's requests. At normal exit,
  the pipeline also applies its shutdown deadline to every pending SDK batch,
  reserving 500 ms for counting failures and flushing metrics. One owned socket
  watcher interrupts a connected request at that cutoff, including a response
  whose headers and body arrive separately. Metrics retain a two-second HTTP
  budget. The SDK owns HTTP retry and backoff.
- The log exporter measures uncompressed protobuf size and splits requests
  above 16 MiB, allowing for the collector's 20 MiB request limit. An individual
  record above the request ceiling is dropped and counted. Harness stderr lines
  are redacted before their text is capped at 4,096 characters.
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

Parent node: [OTLP export backend](telemetry-otlp.md).

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
duplicate-log filter and increments a pending count. The exporter reports the
aggregate outside the emitter at the end of a batch or at shutdown, free of
per-record file I/O in the SDK admission path.

## SDK export loss

The log processor and HTTP log exporter publish through the existing metrics
provider. Provider construction defaults the SDK setting
`OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED` to `true` for the processor.

- `otel.sdk.processor.log.processed` with `error.type=queue_full` counts logs
  evicted by the SDK batch processor.
- `otel.sdk.exporter.log.exported` counts acknowledged records and failed
  records separately. The HTTP adapter reads the final OTLP response so a
  rejected batch carries the HTTP status as `error.type` and partial success
  counts only its rejected records under `error.type="rejected"`. Partial
  acknowledgements complete the request. Failed and rejected counts also go to
  local stderr and process logs.
- These metrics travel through the collector. After an interruption a surviving
  process can export the cumulative counts on a later metrics pass. The local
  diagnostics cover periods when a process exits before its metrics can reach
  the collector.
