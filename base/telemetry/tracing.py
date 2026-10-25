# meiva:note
# The process root owns one admitted SDK arm; billing and public spans observe its shared readiness.
# meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md

from __future__ import annotations

import atexit
import contextlib
import json
import math
import os
import threading
import time
from collections.abc import Callable, Generator, Sequence
from functools import wraps
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from base.cluster.machine import MachineIdentityReader
from base.log import logger
from base.paths import traces_dir
from base.telemetry.delivery.reporting import report_no_pipeline
from base.telemetry.observability import cluster_label
from base.telemetry.otlp.telemetry_otlp import (
    COLLECTOR_RETRY_INTERVAL_S as COLLECTOR_RETRY_INTERVAL_S,
)
from base.telemetry.otlp.telemetry_otlp import (
    endpoint_reachable,
    observability_export_allowed,
)
from base.telemetry.otlp.trace_config import TraceConfig
from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING, TRACE_INIT_WAIT_S, TraceReadiness
from base.telemetry.otlp.trace_workers import CollectorRetry, TraceArm
from base.telemetry.trace_mirror import (
    disk_usage,
    disk_watermark_exceeded,
    enforce_dir_cap,
    gzip_old_mirror,
    mirror_size,
    prune_old_mirror,
)

__all__ = [
    "OtlpJsonHttpSpanExporter",
    "arm_tracing",
    "claim_idle_wait_span",
    "disk_usage",
    "disk_watermark_exceeded",
    "enforce_dir_cap",
    "gzip_old_mirror",
    "initialize_tracing",
    "mirror_size",
    "prune_old_mirror",
    "state",
    "turn_span",
]

# Span attribute keys.
#
# `session.id` — vendor-neutral session grouping key (emerging GenAI semantic
# convention); the consumer groups one agent's spans into a session by it.
_ATTR_NEUTRAL_SESSION_ID = "session.id"

# `meiva.turn` — the per-process turn counter on each turn's root span; orders
# an agent's turns within one process lifetime (it restarts at 1 on respawn;
# cross-process ordering comes from timestamps / checkpoint refs).
_ATTR_TURN = "meiva.turn"

# LangChain/LangGraph node-span naming from the traceloop callback handler:
# every graph node span is `execute_task <node>`. claim_idle_wait_span is only
# allowed to end a span carrying this prefix — inside the claim node that is
# the node's own `execute_task claim` span; ending any other span (the
# enclosing workflow/root span, when the instrumentor is not attached) would
# truncate the turn trace, so the prefix check is the fail-safe boundary.
_NODE_SPAN_PREFIX = "execute_task "

_IDLE_WAIT_SPAN_NAME = "claim idle-wait"

TRACE_EXPORT_TIMEOUT_S = 2.0
TRACE_EXPORT_FAILURE_THRESHOLD = 3
TRACE_EXPORT_COOLDOWN_S = 30.0


# ── LLM content stripping (trace v2) ─────────────────────────────────────────
#
# Span attributes that carry LLM content — the 99.89% of the 2026-08-05 mirror
# blowup (31 GB/h with MEIVA_TRACE_ENABLED=true). Chain metadata lives in the
# `traceloop.association.properties.*` / `gen_ai.task.*` / `langgraph.*`
# attributes and never appears here; these keys are pure payload.
#
# Blacklist (exact keys) + prefix rules (gen_ai.input.* / gen_ai.output.*):
# the openllmetry instrumentors emit prompts/completions under gen_ai.* per
# the GenAI semantic convention; traceloop's wrapper mirrors them as
# traceloop.entity.*.
_CONTENT_ATTR_KEYS: frozenset[str] = frozenset(
    {
        "gen_ai.task.input",
        "gen_ai.task.output",
        "gen_ai.system_instructions",
        "gen_ai.tool_calls",
        "traceloop.entity.input",
        "traceloop.entity.output",
        "messages",
        "system_instructions",
        "inputs",
        "outputs",
    }
)
_CONTENT_ATTR_PREFIXES: tuple[str, ...] = (
    "gen_ai.input.",
    "gen_ai.output.",
)

# Defensive size guard: a metadata attribute is tiny (names, ids, paths,
# durations); anything bigger on a single string value is treated as content
# even if an instrumentor invents a new key — bounds a future leak to a few
# KB per span. 16 KB is above the largest real metadata (a long langgraph
# path array) and far below the smallest real content (a prompt).
MAX_ATTR_STRING_CHARS = 16 * 1024


def _is_content_attribute(key: str) -> bool:
    """True when an attribute key carries LLM content and must not reach disk."""
    if key in _CONTENT_ATTR_KEYS:
        return True
    return any(key.startswith(p) for p in _CONTENT_ATTR_PREFIXES)


def _attribute_value_size(value: dict[str, Any]) -> int:
    """Approximate serialized size of one OTLP AnyValue dict (chars of the
    dominant string payload). Used by the size guard; a content-free
    attribute is a few hundred chars at most."""
    for field in ("stringValue", "bytesValue", "intValue", "doubleValue", "boolValue"):
        if field in value:
            return len(str(value[field]))
    if "arrayValue" in value:
        return sum(_attribute_value_size(v) for v in value["arrayValue"].get("values", []))
    if "kvlistValue" in value:
        return sum(
            _attribute_value_size(kv.get("value", {})) + len(kv.get("key", ""))
            for kv in value["kvlistValue"].get("values", [])
        )
    return 0


def strip_content_attributes(otlp: dict[str, Any]) -> None:
    """Remove LLM-content attributes from a decoded OTLP export request, in place.

    Walks resourceSpans -> scopeSpans -> spans (attributes + events) and drops
    every attribute whose key is a known content key (or content prefix), plus
    any attribute whose string payload exceeds `MAX_ATTR_STRING_CHARS` (the
    size guard — metadata is never large). Span names, timestamps, status,
    ids and `traceloop.*` / `gen_ai.task.*` / `langgraph.*` metadata survive.
    """
    for rs in otlp.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                kept: list[dict[str, Any]] = [
                    kv
                    for kv in span.get("attributes", [])
                    if not _is_content_attribute(kv.get("key", ""))
                    and _attribute_value_size(kv.get("value", {})) <= MAX_ATTR_STRING_CHARS
                ]
                if kept:
                    span["attributes"] = kept
                else:
                    span.pop("attributes", None)
                for ev in span.get("events", []):
                    ev_kept: list[dict[str, Any]] = [
                        kv
                        for kv in ev.get("attributes", [])
                        if not _is_content_attribute(kv.get("key", ""))
                        and _attribute_value_size(kv.get("value", {})) <= MAX_ATTR_STRING_CHARS
                    ]
                    if ev_kept:
                        ev["attributes"] = ev_kept
                    else:
                        ev.pop("attributes", None)


# Module-level mutable state for the idempotent init guard and collector retry.
state: dict[str, Any] = {
    "closed": False,
    "arm_owner": None,
    "retry_owner": None,
    "collector_offline_reported": False,
}
_init_lock = threading.Lock()
_lifecycle_lock = threading.Lock()


class OtlpJsonHttpSpanExporter(SpanExporter):
    """OTLP/HTTP span exporter to the local OTel Collector (protobuf wire).

    Each `export()` batch is encoded to the OTLP dict, content-stripped
    (defensive layer 2 — content never leaves the process), re-serialized to
    the OTLP `ExportTraceServiceRequest` protobuf and POSTed to
    `{endpoint}/v1/traces` with `Content-Type: application/x-protobuf` — the
    same wire path `meiva trace ship` uses. Protobuf, not OTLP/JSON, on the
    wire: the collector's JSON receiver rejects the SDK's padded-base64 ids
    (verified 2026-08-14 against otelcol-contrib 0.155.0). The endpoint is
    the LOCAL sidecar (``MEIVA_TELEMETRY_OTLP_ENDPOINT``, default
    127.0.0.1:4318); agents never dial a backend directly.

    The sidecar is supervised (watchdog, 60s), so a failed POST returns FAILURE
    and lets the SDK's batch processor drop the batch. Three consecutive failed
    batches open a 30-second circuit: exports during cooldown are counted and
    shed without a POST, then one half-open probe decides whether to recover.
    The mirror is the collector's job now; this exporter's only duties are
    stripping (defensive layer 2) and the bounded POST itself.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        strip_content: bool,
        cooldown_s: float = TRACE_EXPORT_COOLDOWN_S,
        timeout_s: float = TRACE_EXPORT_TIMEOUT_S,
    ) -> None:
        self._endpoint = f"{endpoint.rstrip('/')}/v1/traces"
        self._strip_content = strip_content
        self._cooldown_s = cooldown_s
        self._timeout_s = timeout_s
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._probe_in_flight = False
        self._dropped_batches = 0
        self._dropped_spans = 0
        self._circuit_lock = threading.Lock()

    def _circuit_drops(self, span_count: int, *, now: float | None = None) -> bool:
        """Shed during cooldown, admitting only one probe after it expires."""
        current_time = time.monotonic() if now is None else now
        with self._circuit_lock:
            if self._circuit_open_until == 0.0:
                return False
            if current_time >= self._circuit_open_until and not self._probe_in_flight:
                self._probe_in_flight = True
                return False
            self._dropped_batches += 1
            self._dropped_spans += span_count
            dropped_batches = self._dropped_batches
            dropped_spans = self._dropped_spans
        if dropped_batches == 1 or dropped_batches % 50 == 0:
            logger.warning(
                "OTLP trace export circuit open — dropping batch",
                event="trace",
                action="export_dropped",
                endpoint=self._endpoint,
                dropped_batches=dropped_batches,
                dropped_spans=dropped_spans,
            )
        return True

    def export(
        self, spans: Sequence[ReadableSpan], *, now: float | None = None
    ) -> SpanExportResult:
        if self._circuit_drops(len(spans), now=now):
            return SpanExportResult.FAILURE
        # Imported lazily: the OTLP encoder pulls in the protobuf stack, which
        # the no-tracing path should not pay for.
        try:
            from google.protobuf.json_format import MessageToDict, Parse
            from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                ExportTraceServiceRequest,
            )

            otlp = MessageToDict(encode_spans(spans))
            if self._strip_content:
                strip_content_attributes(otlp)
            req = Parse(
                json.dumps(otlp, separators=(",", ":"), ensure_ascii=False),
                ExportTraceServiceRequest(),
            )
            resp = httpx.post(
                self._endpoint,
                content=req.SerializeToString(),
                headers={"Content-Type": "application/x-protobuf"},
                timeout=self._timeout_s,
            )
            resp.raise_for_status()
        except Exception as exc:  # exporter failures must never escape into the SDK
            current_time = time.monotonic() if now is None else now
            with self._circuit_lock:
                self._consecutive_failures += 1
                failures = self._consecutive_failures
                circuit_opened = self._probe_in_flight or failures >= TRACE_EXPORT_FAILURE_THRESHOLD
                self._probe_in_flight = False
                if circuit_opened:
                    self._circuit_open_until = current_time + self._cooldown_s
            if failures == 1 or circuit_opened:
                logger.warning(
                    "OTLP trace export failed",
                    event="trace",
                    action="export_failed",
                    endpoint=self._endpoint,
                    consecutive_failures=failures,
                    circuit_open=circuit_opened,
                    error=repr(exc),
                )
            return SpanExportResult.FAILURE
        with self._circuit_lock:
            recovered_failures = self._consecutive_failures
            dropped_batches = self._dropped_batches
            dropped_spans = self._dropped_spans
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._probe_in_flight = False
        if recovered_failures:
            logger.info(
                "OTLP trace export recovered",
                event="trace",
                action="export_recovered",
                endpoint=self._endpoint,
                consecutive_failures=recovered_failures,
                dropped_batches=dropped_batches,
                dropped_spans=dropped_spans,
            )
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002 — SpanExporter interface signature
        return True

    def shutdown(self) -> None:
        return None


def start_collector_retry(
    machine: MachineIdentityReader, home: Path, config: TraceConfig, *, readiness: TraceReadiness
) -> None:
    with _lifecycle_lock:
        if state.get("closed") or state.get("retry_owner") is not None:
            return
        owner = CollectorRetry(
            initialize=lambda: initialize_tracing(
                machine=machine, home=home, config=config, readiness=readiness
            ),
            resolved=lambda: readiness.resolved,
            interval=lambda: config.retry_interval_s,
        )
        state["retry_owner"] = owner


# meiva:note
# Reset process-level trace state and content capture environment variable.
def reset_tracing_state() -> None:
    os.environ.pop("TRACELOOP_TRACE_CONTENT", None)
    state.clear()
    state.update(
        closed=False,
        collector_offline_reported=False,
        retry_owner=None,
        arm_owner=None,
    )


# meiva:note
# Import traceloop and run Traceloop.init on one daemon thread off the boot
# path (~2.5-3 s for traceloop.sdk + google.genai). Consumers wait on
# the shared recording readiness before first use; a failure logs and resolves the
# wait so recording stays off instead of killing boot.
def _arm_tracing(
    endpoint: str,
    stop_requested: threading.Event | None = None,
    *,
    production: bool,
    home: Path,
    strip_content: bool,
    readiness: TraceReadiness,
) -> None:
    if stop_requested is not None and stop_requested.is_set():
        readiness.close()
        return
    if not readiness.begin():
        return
    recording = False
    try:
        if stop_requested is not None and stop_requested.is_set():
            return
        # meiva:note
        # Content stripping, layer 1 (the source): false-before-init keeps
        # LLM content out of span attributes. Unconditional when
        # strip_content is on — an operator-set true would re-open the
        # 31 GB/h hole. Layer 2 (the exporter) stays armed regardless.
        if strip_content:
            os.environ["TRACELOOP_TRACE_CONTENT"] = "false"

        from traceloop.sdk import Traceloop
        from traceloop.sdk.instruments import Instruments

        # meiva:note
        # Mirror the LLM call paths Meiva actually uses. LangGraph rides on the
        # LANGCHAIN instrumentor (its callback handler nests node spans), so
        # there is no separate LANGGRAPH instrument. LiteLLM, when used,
        # surfaces through the provider instrumentors at the SDK layer.
        instruments = {
            Instruments.ANTHROPIC,
            Instruments.OPENAI,
            Instruments.LANGCHAIN,
            Instruments.GOOGLE_GENERATIVEAI,
        }

        # meiva:note
        # `exporter=` makes OtlpJsonHttpSpanExporter the sole span exporter,
        # pointed at the LOCAL sidecar (no api_endpoint/api_key, so the SDK
        # never dials a remote backend itself); `telemetry_enabled=False`
        # stops traceloop's own usage telemetry from phoning home.
        #
        # Recheck after slow import: a concurrent caller or real shutdown may
        # have resolved admission while this arm waited for the import lock.
        with _init_lock:
            if readiness.resolved or (stop_requested is not None and stop_requested.is_set()):
                return
            Traceloop.init(  # pyright: ignore[reportUnknownMemberType]
                app_name="meiva",
                exporter=OtlpJsonHttpSpanExporter(endpoint, strip_content=strip_content),
                instruments=instruments,
                disable_batch=False,
                telemetry_enabled=False,
                resource_attributes={
                    "cluster": cluster_label(home),
                    "service.line": "meiva",
                    "environment": "prod" if production else "dev",
                },
            )
            if stop_requested is not None and stop_requested.is_set():
                return
            logger.info(
                "trace recording enabled",
                event="trace",
                endpoint=endpoint,
                mirror_dir=str(traces_dir(home)),
            )
            state["collector_offline_reported"] = False
            recording = True
    except BaseException as exc:
        # meiva:note
        # One attempt per process: the retry loop only bridges
        # unreachable->reachable, and retrying Traceloop.init after a partial
        # failure would fake-succeed — the TracerWrapper singleton survives,
        # so a second init() adds no instrumentors yet reports success.
        #
        # SDK failures consume the single arm attempt, including BaseException.
        logger.warning(
            "trace recording failed to initialize — spans disabled this process",
            event="trace",
            action="recording_init_failed",
            error=repr(exc),
        )
    finally:
        readiness.complete(recording=recording)


def arm_tracing(
    endpoint: str,
    stop_requested: threading.Event | None = None,
    *,
    production: bool,
    home: Path,
    strip_content: bool,
    readiness: TraceReadiness,
) -> None:
    if not readiness.reserve():
        return
    _arm_tracing(
        endpoint,
        stop_requested,
        production=production,
        home=home,
        strip_content=strip_content,
        readiness=readiness,
    )


def start_arm_thread(
    config: TraceConfig, machine: MachineIdentityReader, home: Path, *, readiness: TraceReadiness
) -> None:
    with _lifecycle_lock:
        if state.get("closed") or state.get("arm_owner") is not None:
            return
        from base.cluster.derive import production_identity

        production = production_identity(machine, home=home)
        if not readiness.reserve():
            return
        try:
            owner = TraceArm(
                config.endpoint,
                lambda endpoint, stop: _arm_tracing(
                    endpoint,
                    stop,
                    production=production,
                    home=home,
                    strip_content=config.strip_content,
                    readiness=readiness,
                ),
            )
            state["arm_owner"] = owner
        except BaseException:
            readiness.close()
            raise
    try:
        owner.start()
    except BaseException:
        readiness.close()
        owner.request_stop()
        raise


def shutdown(timeout: float = 2.0, *, readiness: TraceReadiness) -> tuple[str, ...]:
    # meiva:note
    # Close admission and report owners whose completion is unconfirmed within one budget.
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("trace stop timeout must be finite and non-negative")
    deadline = time.monotonic() + timeout
    with _lifecycle_lock:
        state["closed"] = True
        readiness.close()
        owners = [owner for key in ("retry_owner", "arm_owner") if (owner := state.get(key))]
    for owner in owners:
        owner.request_stop()
    unfinished: list[str] = []
    primary: BaseException | None = None
    for owner in owners:
        try:
            if not owner.stop(timeout=max(0.0, deadline - time.monotonic())):
                unfinished.append(owner._thread.name)
        except BaseException as exc:
            if primary is None:
                primary = exc
    if unfinished:
        report_no_pipeline(
            "trace shutdown degraded; completion unconfirmed for: {workers}", workers=unfinished
        )
    if primary is not None:
        raise primary
    return tuple(unfinished)


def _recording_ready(readiness: TraceReadiness, *, timeout: float) -> bool:
    reading = readiness.read(timeout=timeout)
    if reading.first_timeout:
        logger.warning(
            "trace init not resolved within timeout — proceeding without recording",
            event="trace",
            action="init_resolved_timeout",
            timeout_s=timeout,
        )
    return reading.ready


def _serialized_initialize[**P](function: Callable[P, None]) -> Callable[P, None]:
    """Apply the module init lock while preserving the public docstring."""

    @wraps(function)
    def serialized(*args: P.args, **kwargs: P.kwargs) -> None:
        with _init_lock:
            function(*args, **kwargs)

    return serialized


# meiva:note
# Initialize OTel span recording to the local collector. Idempotent.
#
# No-op when `config.enabled` is False. When on, this call
# runs only the cheap decisions — disk guards + collector preflight — and
# hands the heavy part (traceloop import + Traceloop.init, ~2.5-3 s) to one
# daemon thread (_arm_tracing), one attempt per process. The boot path is
# therefore sub-second; the ordering contract "instrumentors installed
# before the first turn" is enforced by the shared recording readiness inside
# turn_span (the callback-manager wrap and the SDK instrumentors are
# call-time, so a model built during boot still produces spans). Spans:
# OtlpJsonHttpSpanExporter pointed at the LOCAL sidecar, traceloop
# instrumenting Anthropic + OpenAI + LangChain + Google GenAI (LangGraph
# via the LangChain callback handler).
#
# Recording is one OTLP/HTTP hop to the LOCAL sidecar; the sidecar writes the
# JSONL mirror and either fans out locally or relays to the gateway. If the
# sidecar itself is not answering at init, recording stays off temporarily:
# the episode is logged once and one daemon loop re-checks every five
# minutes until init succeeds or the single arm attempt fails
# permanently (one arm attempt per process).
#
# Guards (each independently configurable):
# - compression (`gzip_old_mirror`): rotated (non-active) mirror segments
#   gzipped first, so retention and the cap see the compressed footprint.
# - retention (`trace_retention_days`): day-stamped mirror files older than
#   N days pruned first (the collector's rotation also bounds them).
# - directory cap (`trace_max_dir_mb`): oldest mirror files deleted until
#   the directory fits under the cap. These three run BEFORE the watermark
#   guard, so an over-watermark disk still gets its relief pass.
# - disk watermark (`trace_disk_watermark`): data disk over the fraction ->
#   recording skipped, warning event emitted (auto-degrade).
# - content stripping (`trace_strip_content`): TRACELOOP_TRACE_CONTENT=false
#   + exporter-side re-strip, so the mirror holds metadata only.
@_serialized_initialize
def initialize_tracing(
    *,
    machine: MachineIdentityReader,
    home: Path,
    config: TraceConfig,
    readiness: TraceReadiness,
    disk_stats: tuple[float, int] | None = None,
) -> None:
    if state.get("closed") or readiness.resolved:
        return
    if not config.enabled or not observability_export_allowed(
        machine, home=home, endpoint_explicit=config.endpoint_explicit
    ):
        return

    # Bounded disk FIRST — before the watermark guard, so the relief valves
    # still run when recording is auto-degraded: compress old segments (so
    # the cap and the day retention see the post-compression footprint),
    # then retention (days), then the hard directory cap. When the disk is
    # already over the watermark this is exactly the pass that frees space —
    # skipping it there would let an over-watermark disk stay full.
    mirror_dir = traces_dir(home)
    mirror_dir.mkdir(parents=True, exist_ok=True)
    if config.gzip_grace_s is None:
        gzip_old_mirror(traces_dir=mirror_dir)
    else:
        gzip_old_mirror(grace_seconds=config.gzip_grace_s, traces_dir=mirror_dir)
    prune_old_mirror(config.retention_days, traces_dir=mirror_dir)
    enforce_dir_cap(config.max_dir_mb, traces_dir=mirror_dir)

    # Disk-watermark guard: if the data disk is over its watermark, skip
    # recording entirely (auto-degrade) instead of letting the mirror grow
    # the disk toward full. Logged as a warning event so the degradation is
    # visible; a later agent start re-checks.
    usage = disk_usage(traces_dir=mirror_dir) if disk_stats is None else disk_stats
    if usage is not None and usage[0] > config.disk_watermark:
        # Auto-degrade: skip recording so the mirror can never fill the disk.
        # The loguru events sink lands this in the unified event stream
        # (event_name='trace', action='recording_disabled_disk_watermark') —
        # visible to ops, never crashes. The warning carries the measured
        # numbers so triage does not need a second command.
        logger.warning(
            "trace recording disabled — data disk over watermark",
            event="trace",
            action="recording_disabled_disk_watermark",
            watermark=config.disk_watermark,
            usage_fraction=round(usage[0], 3),
            free_gb=round(usage[1] / (1024**3), 1),
        )
        return

    # Local-collector preflight: recording IS an OTLP export now, so a missing
    # sidecar means no recording (and no mirror — the mirror is written by the
    # collector). Skip with a warning event instead of arming an exporter that
    # fails every batch. One daemon retry loop re-checks at the exporter retry
    # cadence, while repeated callers in the same episode stay quiet.
    endpoint = config.endpoint
    if not endpoint_reachable(endpoint):
        if not state["collector_offline_reported"]:
            logger.warning(
                "trace recording disabled — local OTel collector not answering",
                event="trace",
                action="recording_disabled_collector_unreachable",
                endpoint=endpoint,
            )
            state["collector_offline_reported"] = True
        start_collector_retry(machine, home, config, readiness=readiness)
        return

    start_arm_thread(config, machine, home, readiness=readiness)


@contextlib.contextmanager
def turn_span(*, name: str, session_id: str, turn: int) -> Generator[None, None, None]:
    # meiva:note
    # Wrap ONE graph invocation (= one agent turn) in an OTel root span.
    #
    # The trace boundary is the turn: the runloop invokes the graph once per
    # turn, so this span opens when the invocation starts — including claim's
    # long wait for the turn's inbound (recorded separately, see
    # claim_idle_wait_span). All child spans (LangGraph nodes, Anthropic calls,
    # etc.) inherit the OTel context and become children of this root, so the
    # recorded mirror shows a proper span tree per turn. The session_id
    # attribute groups one agent's turns into a session on the viewer; the turn
    # attribute orders them within a process lifetime.
    #
    # **Placeholder root — exported at turn START, not at turn end (#1964).**
    # The root span is ended immediately after creation, so the batch processor
    # exports it within one flush (5 s in production, immediately under a
    # SimpleSpanProcessor in tests) while the turn is still running. Residual
    # window: a turn shorter than the flush cadence whose process then dies
    # hard loses the whole unflushed batch (root and children together) — a
    # missing trace, never a rootless one. A trace
    # therefore ALWAYS has its root, even when the process is killed mid-turn
    # (SIGKILL / OOM / crash): the previous behaviour ended the root only when
    # the turn's work finished, so a process that died with the root still open
    # left a rootless trace in Tempo (children export as they end, the root
    # never does). The trade-off: the root's duration reads 0 and its end time
    # is the turn start — the turn's real duration is carried by the child
    # spans (the batch/LLM spans' min start .. max end), which is how
    # Grafana/Tempo compute the trace span anyway. The ended root stays the
    # current span in the OTel context for the whole turn (use_span,
    # end_on_exit=False), so children still parent under it; the context
    # detaches on exit without a second end call.
    #
    # No-op when trace_enabled=False or initialize_tracing hasn't run yet; a
    # pending background init is awaited through shared recording readiness so the
    # root span never opens against the unset proxy tracer.
    #
    if not _recording_ready(PROCESS_RECORDING, timeout=TRACE_INIT_WAIT_S):
        yield
        return
    from opentelemetry import trace as otel_trace

    tracer = otel_trace.get_tracer("meiva.session")
    # Placeholder root: created, attributed and ended at turn start so the
    # trace has its root even if this process dies mid-turn. The context
    # stays attached (end_on_exit=False) so every child span of the turn
    # parents under this span; the CM exit only detaches the context and
    # never ends the span a second time (Span.end is idempotent but warns).
    root = tracer.start_span(name)
    root.set_attribute(_ATTR_NEUTRAL_SESSION_ID, session_id)
    root.set_attribute(_ATTR_TURN, turn)
    root.end()
    with otel_trace.use_span(
        root, end_on_exit=False, record_exception=False, set_status_on_exception=False
    ):
        yield


@contextlib.contextmanager
def claim_idle_wait_span() -> Generator[None, None, None]:
    """Close the claim node span at the park boundary and record the idle wait
    as an explicit `claim idle-wait` span.

    The claim node blocks in `_wait_for_batch` (Redis pub/sub wait + defensive
    SELECT recheck — up to ~30 s per round, unbounded rounds) when an idle
    agent has no inbound. The LangChain instrumentor opened `execute_task
    claim` around the whole node, so without this helper the idle park is
    drawn as one giant opaque node span (observed: 451 s and 697 s traces).
    Ending the node span here makes `execute_task claim` show only the real
    dispatch (ms), and the wait itself stays visible and attributed as a
    labeled `claim idle-wait` span (parented under the ended node span, same
    trace) instead of vanishing.

    The ended span's later counterpart — the instrumentor's `on_chain_end` —
    sees `end_time` already set and skips its own end (its `_end_span` guard),
    so no double-end warning; the node span is exported at the moment the
    wait begins. The handler's post-end `gen_ai.task.status` write is dropped
    by the SDK (one "Setting attribute on ended span." log line per park —
    information-free, kept in the log files, filtered out of the event stream
    by `base/log/__init__.py:_event_pipeline_filter`).

    No-op when trace_enabled=False or initialize_tracing hasn't run yet, and —
    defensively — when the current span is not recording or is not a LangChain
    node span (`execute_task ...`). The helper may only end the claim node's
    own span; with the instrumentor absent the wait simply stays inside
    whatever span is current (the pre-fix behavior).
    """
    if not _recording_ready(PROCESS_RECORDING, timeout=TRACE_INIT_WAIT_S):
        yield
        return
    from opentelemetry import trace as otel_trace

    current = otel_trace.get_current_span()
    if not current.is_recording():
        yield
        return
    # `name` is SDK-only (not on the otel API Span type, hence the cast); the
    # runtime object is the SDK span the callback handler started, and the
    # prefix check is the fail-safe that keeps us from ending the enclosing
    # workflow/root span when the node instrumentor is not attached.
    span_name = getattr(current, "name", "")
    if not isinstance(span_name, str) or not span_name.startswith(_NODE_SPAN_PREFIX):
        yield
        return
    current.end()
    tracer = otel_trace.get_tracer("meiva.claim")
    with tracer.start_as_current_span(_IDLE_WAIT_SPAN_NAME):
        yield


atexit.register(shutdown, readiness=PROCESS_RECORDING)
