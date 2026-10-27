"""OTLP export backend — the write side of the OTel + Tempo/Loki/Prometheus stack.

Exports unified emitter batches when the startup OTLP flag is enabled. The
JSONL mirror precedes this ordinary observation sink; OTLP does not replace it.

The endpoint (``MEIVA_TELEMETRY_OTLP_ENDPOINT``, default 127.0.0.1:4318) is the LOCAL OTel
Collector sidecar on every machine (task #1266, 2026-08-14): agents never dial a backend
directly. A gateway collector fans out logs -> loopback Loki and metrics -> loopback
Prometheus; a pure runner collector relays them to the gateway collector's authenticated
private-address OTLP receiver. A remote agent keeps the localhost producer endpoint — its
first hop is still its own sidecar. Three signals:

- **logs** — every ``Event`` becomes one OTLP LogRecord (Loki). The body is the
  full event as JSON (the same shape the JSONL mirror stores, so the mirror and
  Loki hold the same content class); the indexed dimensions
  (event_name / category / level / machine / process / source / agent ids) ride
  as attributes; event_name and agent_id also select each record's resource so
  Loki can index them without mixing event types in one resource batch;
  ``trace_id`` / ``span_id`` fill the LogRecord fields so logs correlate with
  Tempo spans.
- **metrics** — telemetry-category events become OTLP metrics (Prometheus).
  Each numeric payload field maps to one instrument named
  ``meiva_<event_name>_<field>``: int -> Counter, float -> Histogram, with
  explicitly declared absolute-state fields exported as Observable Gauges
  (see ``_record_metrics`` for the rules); datapoint attributes are the
  process dimensions + declared payload scalars only (never loguru decoration
  extras — a per-event msg string would split every counter into its own
  series). Log/audit events produce no metrics: they are the event stream,
  not a measurement.
- **traces** — NOT exported here. ``base/telemetry/tracing.py`` exports them to the same
  local collector, whose file exporter writes the standard OTLP/JSON mirror.
  ``meiva trace ship`` is the separate recovery replay: gateway units dial Tempo
  directly; pure runners use the authenticated gateway collector ingress.

Exporter log mapping and SDK failures retain their named isolation scopes.
The owned writer keeps provider creation, flush and teardown off exit callers;
finite observations report unfinished work without claiming SDK termination.

Export defaults require a registered production machine; explicit endpoints
allow other homes. Startup-frozen settings control enablement. First construction
is refused during interpreter finalization; disabled export keeps the JSONL mirror.

Child deferral (task #3816 M4b): an exec child arms `defer_until_exit()` before
its first record; batches are held in the bounded queue — OTel stack, settings
chain, and metric plumbing unimported — and complete at `finalize()` (clean
exit), on hold saturation, or at the max-age bound. The policy and its
semantics live in `base.telemetry.otlp.telemetry_otlp_defer`.

"""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from base.cluster.derive import production_identity
from base.cluster.machine import MachineIdentityReader
from base.telemetry.delivery.defer_plan import ChildDeferPlan
from base.telemetry.delivery.loss import report_loss
from base.telemetry.delivery.mirror import append_jsonl
from base.telemetry.delivery.receipts import DrainPhase, DrainResult, DrainStatus, Event
from base.telemetry.delivery.reporting import (
    failure_isolated,
    report_no_pipeline,
    report_sink_failure,
)
from base.telemetry.emitter import emit, pipeline_bound
from base.telemetry.metrics import ci_runs_metrics
from base.telemetry.observability import gateway_observability_home
from base.telemetry.otlp import telemetry_otlp_metrics
from base.telemetry.otlp.plans import (
    BASE_METRIC_DISPOSITION,
    select_metric_attributes,
)
from base.telemetry.otlp.telemetry_otlp_defer import ChildDeferral
from base.telemetry.otlp.telemetry_otlp_gauges import (
    GaugeValues,
    observable_gauge_callback,
    record_gauge,
)
from base.telemetry.otlp.telemetry_otlp_logs import emit_log_record
from base.telemetry.otlp.telemetry_otlp_metrics import (
    EVENT_LOOP_LAG_BUCKETS_MS as EVENT_LOOP_LAG_BUCKETS_MS,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    LLM_LATENCY_BUCKETS_MS as LLM_LATENCY_BUCKETS_MS,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    EventDimensionResourceExporter as EventDimensionResourceExporter,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    MetricsResource,
    build_providers,
    strip_unit_suffix,
    unit_for,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    metric_views as metric_views,
)
from base.telemetry.otlp.telemetry_otlp_worker import OtlpWorker

__all__ = [
    "COLLECTOR_RETRY_INTERVAL_S",
    "OtlpBackend",
    "bind_exporter",
    "default_exporter",
    "endpoint_reachable",
    "observability_export_allowed",
    "remaining_time",
]


# meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md
def remaining_time(deadline: float, now: float) -> float:
    return max(0.0, deadline - now)


# ── knobs ─────────────────────────────────────────────────────────────────────

# Bounded queue between the emitter drain thread and the OTLP worker. The SDK's
# own batch processors ALSO have a bounded queue (2048) and their emit() blocks
# when full — so this barrier must be no larger than the SDK's, and it is what
# turns "OTLP exporter thread stuck on a hung endpoint" into counted shedding
# instead of a stalled drain thread.
_QUEUE_MAXSIZE = 2048

# A failed collector probe costs up to 1.5 seconds. Retry on the drain thread
# every five minutes, not on every batch, so a missing sidecar cannot turn event
# volume into connection-probe volume.
COLLECTOR_RETRY_INTERVAL_S = 300
NO_EMITTER = "_no_emitter"

METRIC_DISPOSITION: dict[tuple[str, str], str | None] = {
    **BASE_METRIC_DISPOSITION,
    **ci_runs_metrics.CI_RUN_GAUGE_DISPOSITIONS,
}


# meiva:note
# Decide export eligibility from the caller's machine, unit home and environment.
def observability_export_allowed(
    machine: MachineIdentityReader, *, home: Path, endpoint_explicit: bool
) -> bool:
    if endpoint_explicit:
        return True
    if not production_identity(machine, home=home):
        return False
    gateway_home = gateway_observability_home(machine, home=home)
    if gateway_home is None:
        return True
    from base.telemetry.observability import home_is_observability_station

    allowed = home_is_observability_station(gateway_home, machine=machine, unit_home=home)
    if not allowed:
        from base.log import logger

        logger.bind(**{NO_EMITTER: True}).warning(
            "[otlp-exporter] OTLP export disabled: gateway home is not the "
            "observability station (no {} marker and no observability-station "
            "capability); set MEIVA_TELEMETRY_OTLP_ENDPOINT to use an explicit collector",
            gateway_home / "lgtm-host",
        )
    return allowed


def endpoint_reachable(endpoint: str) -> bool:
    """One quick preflight against an OTLP collector. Any HTTP answer proves a
    listener is up — including 4xx/5xx, which ``urlopen`` raises as
    HTTPError (the collector answers /v1/logs with 415 unless the body
    carries an OTLP content type). Only connection-level failures mean no
    collector (a fresh install without the LGTM stack), where building the
    SDK exporters would make their own threads log 'Exception while
    exporting' every interval forever. The probe posts an OTLP/JSON body
    so a healthy collector answers 200. A failed probe is retried every five
    minutes; disabled and recovered episodes are also reported as real events.

    Shared by the events exporter (here) and the trace exporter
    (``base.telemetry.tracing.initialize_tracing``) — both arm their SDK exporters
    against the same local sidecar.

    2026-08-12 prod incident: this probe sent no Content-Type, so urllib
    defaulted to application/x-www-form-urlencoded, the collector answered
    415, HTTPError was suppressed as "not answering", and every process
    that restarted after the #1214 rollout silently disabled its OTLP
    export — no events in Loki, no meiva_* metrics in Prometheus.
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urljoin, urlsplit

    # Only http(s) endpoints can carry an OTLP collector; anything else
    # (file:, a bare host) is not something this probe should open.
    if urlsplit(endpoint).scheme not in ("http", "https"):
        return True
    try:
        url = urljoin(endpoint.rstrip("/") + "/", "v1/logs")
        req = urllib.request.Request(  # noqa: S310 — scheme validated above; deliberate one-shot preflight
            url,
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1.5):  # noqa: S310 — same validated probe
            return True
    except urllib.error.HTTPError:
        # Any HTTP status proves a listener answered the port — a 415
        # from a collector that rejects the probe's content type is still
        # the collector.
        return True
    except OSError:  # URLError, refused / reset / timed-out connections: nothing answered
        return False


def _emit_backend_event(event_name: str, **attributes: object) -> None:
    """Emit init status into the unified stream; never affect backend setup.

    When the collector is unavailable, the event reaches the JSONL mirror even
    though its OTLP copy cannot leave the process.
    """
    with failure_isolated("otlp init-status event"):
        emit("telemetry", event_name, attributes=attributes)


def _observability_settings() -> tuple[bool, str]:
    from base.config import settings

    return (
        bool(settings.observability.telemetry_otlp_enabled),
        settings.observability.telemetry_otlp_endpoint,
    )


class _Instrument(Protocol):
    add: Callable[..., Any]
    record: Callable[..., Any]


class OtlpBackend:
    """One owned OTLP export backend: bounded queue + worker thread that
    feeds the OTel SDK log processor, plus direct in-memory metric recording.

    ``providers`` is the test seam — an injected (LoggerProvider,
    MeterProvider) pair wired to in-memory exporters; production builds the
    real OTLP/HTTP pair from settings (``build_providers``).
    """

    def __init__(
        self,
        *,
        export_allowed: Callable[[], bool],
        providers: tuple[Any, Any] | None = None,
        queue_maxsize: int = _QUEUE_MAXSIZE,
        enabled: Callable[[], bool] | None = None,
        endpoint: str | None = None,
        retry_interval_s: float = COLLECTOR_RETRY_INTERVAL_S,
        mirror_dir: Path,
        home: Path | None = None,
    ) -> None:
        self._export_allowed = export_allowed
        self._enabled_flag = enabled
        self._endpoint_override = endpoint
        self._retry_interval_s = retry_interval_s
        self._mirror_dir = mirror_dir
        self._home = home
        self._export_decision: bool | None = None
        self._eligibility_lock = threading.Lock()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_maxsize)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._providers = providers
        self._logs: Any = None  # LoggerProvider (any-typed: OTel SDK imported lazily)
        self._metric_provider: Any = None
        self._meter: Any = None
        self._instruments: dict[tuple[str, str], Any] = {}
        self._gauge_values: GaugeValues = {}
        self._gauge_lock = threading.Lock()
        self._init_failed_at: float | None = None
        self._init_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._worker: OtlpWorker | None = None
        self._closed = threading.Event()
        self._admission_lock = threading.Lock()
        self._producers_idle = threading.Event()
        self._producers_idle.set()
        self._active_producers = 0
        self._start_error: BaseException | None = None
        # meiva:note
        # Set when the owner starts closing. A bring-up failure after this point is only
        # reportable if the process bound an event pipeline to carry the report (#5578).
        # meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md
        self._closing = False
        # Child deferral (task #3816 M4b) — policy in base.telemetry.otlp.telemetry_otlp_defer;
        # the lambdas look the backend methods up per call so test seams stay live.
        self._deferral = ChildDeferral(
            queue=self._queue,
            bring_up=lambda: self._enabled() and self._ensure(),
            record_metrics=lambda event: self._record_metrics(event),  # noqa: PLW0108 — per-call lookup keeps owner seams live
            export_live=lambda events: self._export_live(events),  # noqa: PLW0108 — per-call lookup keeps owner seams live
        )

    # ── public surface (called by base.telemetry) ─────────────────────────

    def export_batch(self, events: list[Event]) -> None:
        """Export one emitter batch; required identity defects propagate.
        Transport failures stay isolated. Logs enqueue (shed when full),
        while metrics record in memory (lock-free atomics).

        While deferred (exec-child arm, task #3816 M4b) the batch is held in the
        same bounded queue with no worker — no settings read, no OTel import —
        until saturation, the max-age timer, or finalize()/shutdown()."""
        if not events or self._closed.is_set():
            return
        if self._deferral.is_active() and self._logs is None:
            with self._admission_lock:
                if self._closed.is_set():
                    return
                self._active_producers += 1
                self._producers_idle.clear()
            try:
                self._deferral.hold_batch(events)
            finally:
                self._producer_finished()
            return
        if not self._enabled() or not self._ensure():
            return
        self._export_live(events)

    def _export_live(self, events: list[Event]) -> None:
        """Enqueue one batch on the live path + record its metrics.

        Split out of export_batch so the deferred hand-off (saturation,
        finalize) reuses the exact live semantics — including the counted shed
        when the queue stays full."""
        with self._admission_lock:
            if self._closed.is_set():
                return
            self._active_producers += 1
            self._producers_idle.clear()
        try:
            self._publish_live(events)
        finally:
            self._producer_finished()

    def _producer_finished(self) -> None:
        with self._admission_lock:
            self._active_producers -= 1
            if not self._active_producers:
                self._producers_idle.set()

    def _publish_live(self, events: list[Event]) -> None:
        """Record admitted events; teardown retains providers until this SDK use ends."""
        lost = 0
        example = None
        for event in events:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                lost += 1
                example = event
        if lost and example is not None:
            with self._dropped_lock:
                self._dropped += lost
            report = report_loss(example, lost, "otlp", record_metric=self.record_loss)
            # The alarm itself bypasses both queues. Metrics can still export
            # while the log lane is full; the local mirror retains the evidence.
            append_jsonl([report], target_dir=self._mirror_dir)
        for event in events:
            with failure_isolated("otlp metric mapping"):
                self._record_metrics(event)

    def flush(self, timeout: float = 2.0) -> DrainResult:
        """A finite receipt from the sole SDK writer, including provider flush."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("OTLP timeout must be finite and non-negative")
        worker = self._worker
        if worker is None:
            if self._start_error is not None:
                raise self._start_error
            completed = self._queue.empty()
            return DrainResult(
                DrainStatus.COMPLETED if completed else DrainStatus.UNFINISHED, DrainPhase.DRAIN
            )
        return worker.sync(timeout)

    # meiva:note
    # The pipeline calls this before its own drain: that drain is where a never-started
    # exporter gets brought up for the first time at interpreter exit, an attempt the SDK
    # can no longer honour. Marking it keeps that failure out of a log nobody can act on.
    # meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md
    def begin_close(self) -> None:
        self._closing = True

    def shutdown(self, timeout: float = 2.0) -> DrainResult:
        """Close admission and observe this exporter within one local deadline."""
        self.begin_close()
        self.begin_close()
        # meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("OTLP timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        worker, preparation_error = self._close_admission()
        deferred_finished = self._deferral.stop(timeout=remaining_time(deadline, time.monotonic()))
        if self._start_error is not None:
            raise self._start_error
        if worker is None:
            if preparation_error is not None:
                raise preparation_error
            completed = self._queue.empty() and deferred_finished and self._producers_idle.is_set()
            if not completed:
                self._report("OTLP shutdown degraded; deferred records remain unfinished")
            return DrainResult(
                DrainStatus.COMPLETED if completed else DrainStatus.UNFINISHED, DrainPhase.STOP
            )
        result = worker.stop(timeout=remaining_time(deadline, time.monotonic()))
        if preparation_error is not None:
            raise preparation_error
        if not deferred_finished:
            self._report("OTLP shutdown degraded; child deferral remains unfinished")
            return DrainResult(DrainStatus.UNFINISHED, DrainPhase.STOP)
        return result

    def _close_admission(self) -> tuple[OtlpWorker | None, BaseException | None]:
        """Keep a deferred attempt asynchronous, then stop even if preparation failed."""
        # Deferred bring-up retains its enabled gate and finite observation owner.
        preparation_error: BaseException | None = None
        try:
            if (
                self._deferral.is_active()
                and not self._queue.empty()
                and not self._closed.is_set()
                and self._enabled()
            ):
                self._get_worker()
        except BaseException as exc:
            preparation_error = exc
        with self._admission_lock:
            self._closed.set()
            worker = self._worker
            if worker is not None:
                worker.request_stop()
        return worker, preparation_error

    # ── child deferral (task #3816 M4b) ─────────────────────────────────────

    def defer_until_exit(self, plan: ChildDeferPlan) -> None:
        """Arm a cold child hold; flag-off and already-live backends are no-ops."""
        if self._logs is not None:
            return
        self._deferral.arm(plan)

    def finalize(self, *, finalizing: bool = False) -> None:
        """Complete an ordinary deferred hold, then observe its FIFO flush.

        Failed bring-up preserves the hold under the existing retry gate; an
        empty hold constructs no SDK resources. Shutdown has its finite seam.
        """
        if finalizing or sys.is_finalizing():
            return
        if self._deferral.is_active():
            self._deferral.complete("finalize")
        self.flush()

    # ── flag + backend bring-up ──────────────────────────────────────────────

    def _enabled(self) -> bool:
        """Keep transport settings isolated; identity defects remain caller errors."""
        if not self._kill_switch():
            return False
        with self._eligibility_lock:
            if self._export_decision is None:
                self._export_decision = self._export_allowed()
            return self._export_decision

    def _kill_switch(self) -> bool:
        if self._enabled_flag is not None:
            return self._enabled_flag()
        try:
            enabled, _endpoint = _observability_settings()
            return enabled
        except Exception as exc:
            report_sink_failure("otlp enabled-flag read", exc)
            return False

    def warmup(self) -> None:
        """Resolve eligibility before isolating lazy SDK and network preparation."""
        if self._enabled():
            with failure_isolated("otlp warmup"):
                self._ensure()

    def deferred_state(self) -> bool:
        """Whether this owner's backend holds an exec-child backlog."""
        return self._deferral.is_active()

    def record_loss(self, report: Event) -> None:
        """Record a loss metric only when this owner already has a meter."""
        if self._meter is not None:
            self._record_metrics(report)

    @staticmethod
    def _endpoint_reachable(endpoint: str) -> bool:
        """Module-level ``endpoint_reachable`` — see its docstring."""
        return endpoint_reachable(endpoint)

    def _ensure(self, *, finalizing: bool = False) -> bool:
        """Resolve one lazy exporter attempt; a real stop wakes a stuck init waiter."""
        worker = self._get_worker(finalizing=finalizing)
        return worker is not None and worker.wait_ready()

    def _get_worker(self, *, finalizing: bool = False) -> OtlpWorker | None:
        if (
            self._closed.is_set()
            or finalizing
            or sys.is_finalizing()
            or self._start_error is not None
        ):
            return None
        with self._init_lock, self._admission_lock:
            if self._closed.is_set():
                return None
            if self._worker is not None:
                if self._worker._thread.is_alive():
                    return self._worker
                self._worker._check_error()
                if not self._worker._cleanup_complete:
                    return self._worker  # retain uncertain SDK resources across retry opportunities
            if (
                self._init_failed_at is not None
                and time.monotonic() - self._init_failed_at < self._retry_interval_s
            ):
                return None
            try:
                worker = OtlpWorker(
                    event_queue=self._queue,
                    initialize=self._initialize_worker,
                    emit_log=self._emit_log,
                    record_metrics=self._record_metrics,
                    producers_idle=self._producers_idle,
                    deferred_idle=self._deferral.completion_idle,
                    deferred_active=self._deferral.is_active,
                )
                self._worker = worker
                self._thread = worker._thread
            except BaseException as exc:
                self._start_error = exc
                report_sink_failure("otlp worker start", exc)
                return None
        return worker

    def _initialize_worker(self, worker: OtlpWorker) -> bool:
        """Keep the existing named SDK init isolation and every partial resource."""
        endpoint: str | None = self._endpoint_override
        disabled_at = self._init_failed_at
        try:
            if self._providers is not None:
                worker.logs, worker.metrics = self._providers
            else:
                if endpoint is None:
                    _enabled, endpoint = _observability_settings()
                if not self._endpoint_reachable(endpoint):
                    self._init_failed_at = time.monotonic()
                    self._report(
                        f"OTLP endpoint {endpoint} not answering — OTLP export disabled; retrying in {self._retry_interval_s}s"
                    )
                    _emit_backend_event(
                        "otlp_backend_disabled", reason="endpoint not answering", endpoint=endpoint
                    )
                    return False

                def keep_logs(provider: object) -> None:
                    worker.logs = provider

                def keep_reader(reader: object) -> None:
                    worker.metric_reader = reader

                def keep_metrics(provider: object) -> None:
                    worker.metrics = provider

                worker.logs, worker.metrics = build_providers(
                    endpoint,
                    keep_logs=keep_logs,
                    keep_reader=keep_reader,
                    keep_metrics=keep_metrics,
                    mirror_dir=self._mirror_dir,
                    home=self._home,
                )
            meter = worker.metrics.get_meter("meiva.telemetry")
            self._logs, self._metric_provider, self._meter = worker.logs, worker.metrics, meter
        except Exception as exc:
            # meiva:note
            # A process that binds no event pipeline has no sink for this diagnosis — the
            # attempt could not deliver anything either. That is the watcher child's shape:
            # it builds the boot pipeline, binds none, and at exit the SDK's executors are
            # already shut down, so the bring-up fails and used to print a warning plus
            # traceback for work that finished (#5578).
            # meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md
            if self._closing and not pipeline_bound():
                return False
            self._init_failed_at = time.monotonic()
            self._report(
                f"OTLP backend init failed — OTLP side disabled; retrying in {self._retry_interval_s}s: {exc!r}"
            )
            _emit_backend_event(
                "otlp_backend_disabled", reason=f"init failed: {exc!r}", endpoint=endpoint
            )
            return False
        self._init_failed_at = None
        if disabled_at is not None:
            _emit_backend_event(
                "otlp_backend_recovered",
                endpoint=endpoint,
                disabled_s=max(0.0, time.monotonic() - disabled_at),
            )
        return True

    # ── signal mapping ───────────────────────────────────────────────────────

    def _emit_log(self, event: Event) -> None:
        """Map one Event to an OTLP LogRecord and emit it.

        The mapping body lives in `base.telemetry.otlp.telemetry_otlp_logs` (split for the
        800-line ceiling); runs on the worker thread (or flush()).
        """
        emit_log_record(self._logs, event)

    def _record_metrics(self, event: Event) -> None:
        """Map a telemetry event's numeric payload fields to OTLP instruments.

        Rules (deliberately simple, documented):
        - telemetry category only — log/audit events are the event stream, not
          a measurement.
        - int payload field -> Counter (token counts, event counts: things you
          sum). float -> Histogram (latencies/durations: things you
          percentile). `METRIC_DISPOSITION` overrides per field: a float that
          is really a sum (cost_usd) records as a Counter; an absolute state
          (resolution_status) records an ObservableGauge; a rate snapshot
          (price_*) records nothing.
        - bool / short-str payload fields become datapoint attributes (model,
          ok, fn); `body` and strings over the length cap never do (content /
          cardinality guard).
        - None values are skipped (an absent optional metric is not zero).
        """
        if event.category != "telemetry":
            return
        attrs = self._metric_attributes(event)
        for key, value in event.attributes.items():
            # bool must be checked before int — `isinstance(True, int)` is True.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            default_kind = "counter" if isinstance(value, int) else "histogram"
            kind = METRIC_DISPOSITION.get((event.event_name, key), default_kind)
            if kind is None:
                continue
            inst = self._instrument(event.event_name, key, kind)
            if inst is None:
                continue
            if kind == "counter":
                inst.add(value, attrs)
            elif kind == "histogram":
                inst.record(value, attrs)
            else:
                record_gauge(
                    self._gauge_values,
                    self._gauge_lock,
                    (event.event_name, key),
                    value,
                    attrs,
                    max_only=(event.event_name, key) == ("event_log_drop", "last_dropped_at"),
                )

    def _metric_attributes(self, event: Event) -> dict[str, Any]:
        """The datapoint attribute set: process dimensions + declared payload
        scalars that pass the content/cardinality guard.

        Only payload-declared keys (`base.events.contract.payload_keys`)
        become attributes — loguru decoration extras (msg, cache_pct,
        reason_pct, ...) are content, not dimensions: a per-event unique
        string (msg) would split every event into its own series, and a
        counter split into single-sample series reads as zero increments
        (increase() cannot see them). An event with no declared payload
        contributes no extra attributes."""
        from base.events.contract import payload_keys

        return select_metric_attributes(
            machine=event.machine,
            process=event.process,
            agent_id=event.agent_id,
            attributes=event.attributes,
            allowed_keys=payload_keys(event.event_name),
        )

    def _instrument(self, event_name: str, field: str, kind: str) -> _Instrument | None:
        """Lazily create (and cache) the instrument for one (event, field).

        Returns None (and reports) on a creation conflict — e.g. two event/
        field pairs collapsing onto one metric name with different kinds —
        so one bad pair sheds only its own metrics, never the batch."""
        key = (event_name, field)
        inst: Any = self._instruments.get(key)
        if inst is not None:
            return inst
        name = f"meiva_{event_name}_{strip_unit_suffix(field)}"
        try:
            if kind == "counter":
                inst = self._meter.create_counter(
                    name,
                    unit=unit_for(field),
                    description=f"{event_name}.{field} — OTLP-mapped from the unified event stream",
                )
            elif kind == "histogram":
                inst = self._meter.create_histogram(
                    name,
                    unit=unit_for(field),
                    description=f"{event_name}.{field} — OTLP-mapped from the unified event stream",
                )
            else:
                inst = self._meter.create_observable_gauge(
                    name,
                    callbacks=[
                        observable_gauge_callback(
                            self._gauge_values, self._gauge_lock, (event_name, field)
                        )
                    ],
                    unit=unit_for(field),
                    description=f"{event_name}.{field} — OTLP-mapped absolute state from the unified event stream",
                )
        except Exception as exc:  # report once per pair, skip the pair
            self._report(f"OTLP metric instrument {name!r} creation failed: {exc!r}")
            return None
        self._instruments[key] = inst
        return inst

    # ── diagnostics ──────────────────────────────────────────────────────────

    def _report(self, message: str) -> None:
        """Log an OTLP-side diagnostic through loguru, marked `NO_EMITTER` so
        it reaches stderr/file sinks and never re-enters the event pipeline.
        Best-effort: never raises, falls back to stderr."""
        report_no_pipeline(f"[otlp-exporter] {message}")

    _bound_default: OtlpBackend | None = None


# meiva:note
# Temporarily bind ``exporter`` as the default backend for new pipelines.
@contextmanager
def bind_exporter(exporter: OtlpBackend) -> Generator[None]:
    previous = OtlpBackend._bound_default
    OtlpBackend._bound_default = exporter
    try:
        yield
    finally:
        OtlpBackend._bound_default = previous


# meiva:note
# Return the bound exporter override if active, otherwise construct ``OtlpBackend``.
def default_exporter(*, export_allowed: Callable[[], bool], mirror_dir: Path) -> OtlpBackend:
    bound = OtlpBackend._bound_default
    if bound is not None:
        return bound
    return OtlpBackend(export_allowed=export_allowed, mirror_dir=mirror_dir)


# meiva:note
# Build the metric resource for ``home`` (or the active cluster home).
def metrics_resource(*, home: Path | None = None) -> MetricsResource:
    return telemetry_otlp_metrics.metrics_resource(home=home)
