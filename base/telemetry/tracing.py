# meiva:note
# The process root owns one admitted SDK arm; billing and public spans observe its shared readiness.
# meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.md

from __future__ import annotations

import atexit
import contextlib
import json
import math
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import TypedDict

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from base.cluster.machine import MachineIdentityReader
from base.log import logger
from base.telemetry.delivery.reporting import report_no_pipeline
from base.telemetry.observability import (
    cluster_label,
    endpoint_override_is_explicit,
    observability_export_allowed,
)
from base.telemetry.otlp.telemetry_otlp import (
    COLLECTOR_RETRY_INTERVAL_S as COLLECTOR_RETRY_INTERVAL_S,
)
from base.telemetry.otlp.telemetry_otlp import endpoint_reachable
from base.telemetry.otlp.trace_config import TraceConfig, TraceSettings
from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING, TRACE_INIT_WAIT_S, TraceReadiness
from base.telemetry.otlp.trace_strip import strip_content_attributes
from base.telemetry.otlp.trace_workers import CollectorRetry, TraceArm

__all__ = [
    "OtlpJsonHttpSpanExporter",
    "arm_tracing",
    "claim_idle_wait_span",
    "initialize_tracing",
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


class _TraceState(TypedDict):
    closed: bool
    arm_owner: TraceArm | None
    retry_owner: CollectorRetry | None
    collector_offline_reported: bool
    traceloop_init: Callable[..., object] | None


state: _TraceState = {
    "closed": False,
    "arm_owner": None,
    "retry_owner": None,
    "collector_offline_reported": False,
    "traceloop_init": None,
}
_init_lock = threading.Lock()
_lifecycle_lock = threading.Lock()


class OtlpJsonHttpSpanExporter(SpanExporter):
    """OTLP/HTTP span exporter to the local OTel Collector (protobuf wire).

    Each `export()` batch is encoded to the OTLP dict, content-stripped
    (defensive layer 2 — content never leaves the process), re-serialized to
    the OTLP `ExportTraceServiceRequest` protobuf and POSTed to
    `{endpoint}/v1/traces` with `Content-Type: application/x-protobuf`.
    Protobuf, not OTLP/JSON, on the wire: the collector's JSON receiver rejects the SDK's padded-base64 ids
    (verified 2026-08-14 against otelcol-contrib 0.155.0). The endpoint is
    the LOCAL sidecar (``MEIVA_TELEMETRY_OTLP_ENDPOINT``, default
    127.0.0.1:4318); agents never dial a backend directly.

    The sidecar is supervised (watchdog, 60s), so a failed POST returns FAILURE
    and lets the SDK's batch processor drop the batch. Three consecutive failed
    batches open a 30-second circuit: exports during cooldown are counted and
    shed without a POST, then one half-open probe decides whether to recover.
    This exporter's only duties are
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
        try:
            from google.protobuf.json_format import MessageToDict, Parse
            from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                ExportTraceServiceRequest,
            )

            otlp = MessageToDict(encode_spans(spans))
            strip_content_attributes(otlp, strip_llm=self._strip_content)
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


def build_process_tracer(
    *,
    service: str,
    machine: MachineIdentityReader,
    home: Path,
    settings: TraceSettings,
    environ: Mapping[str, str],
) -> TracerProvider | None:
    """Build a lightweight provider for a process without LangGraph instrumentation."""
    if not settings.trace_enabled or not observability_export_allowed(
        machine,
        home=home,
        endpoint_explicit=endpoint_override_is_explicit("MEIVA_TELEMETRY_OTLP_ENDPOINT", environ),
    ):
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service, "service.line": "meiva", "cluster": cluster_label(home)}
        ),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OtlpJsonHttpSpanExporter(
                settings.telemetry_otlp_endpoint, strip_content=settings.trace_strip_content
            )
        )
    )
    return provider


def start_collector_retry(
    machine: MachineIdentityReader,
    home: Path,
    config: TraceConfig,
    *,
    readiness: TraceReadiness,
    now: Callable[[], datetime],
) -> None:
    with _lifecycle_lock:
        if state.get("closed") or state.get("retry_owner") is not None:
            return
        owner = CollectorRetry(
            initialize=lambda: initialize_tracing(
                machine=machine, home=home, config=config, readiness=readiness, now=now
            ),
            resolved=lambda: readiness.resolved,
            interval=lambda: config.retry_interval_s,
        )
        state["retry_owner"] = owner


def _call_traceloop_init(default_init: Callable[..., object], **kwargs: object) -> None:
    (state.get("traceloop_init") or default_init)(**kwargs)


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
        # Content stripping, layer 1 (the source) is the process root's job:
        # `TraceConfig.traceloop_environment()` names the SDK's one content switch,
        # and the root sets it before this runs. Layer 2 (the exporter) stays armed
        # regardless.
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
            _call_traceloop_init(
                Traceloop.init,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
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
        named = (("trace-collector-retry", state["retry_owner"]), ("trace-arm", state["arm_owner"]))
    owners = [(name, owner) for name, owner in named if owner is not None]
    for _, owner in owners:
        owner.request_stop()
    unfinished: list[str] = []
    primary: BaseException | None = None
    for name, owner in owners:
        try:
            if not owner.stop(timeout=max(0.0, deadline - time.monotonic())):
                unfinished.append(name)
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
# runs only the cheap decision — the collector preflight — and
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
# Recording is one OTLP/HTTP hop to the LOCAL sidecar, which relays to Tempo (directly or through
# the gateway). Traces are telemetry: lossy by design, with no local copy and no replay. If the
# sidecar itself is not answering at init, recording stays off temporarily:
# the episode is logged once and one daemon loop re-checks every five
# minutes until init succeeds or the single arm attempt fails
# permanently (one arm attempt per process).
#
# Content stripping (`trace_strip_content`): the root sets TRACELOOP_TRACE_CONTENT=false
# (`TraceConfig.traceloop_environment`) + exporter-side re-strip, so recorded spans hold
# metadata only.
@_serialized_initialize
def initialize_tracing(
    *,
    machine: MachineIdentityReader,
    home: Path,
    config: TraceConfig,
    readiness: TraceReadiness,
    now: Callable[[], datetime],
) -> None:
    if state.get("closed") or readiness.resolved:
        return
    if not config.enabled or not observability_export_allowed(
        machine, home=home, endpoint_explicit=config.endpoint_explicit
    ):
        return

    # Local-collector preflight: recording IS an OTLP export, so a missing
    # sidecar means no recording. Skip with a warning event instead of arming an exporter that
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
        start_collector_retry(machine, home, config, readiness=readiness, now=now)
        return

    start_arm_thread(config, machine, home, readiness=readiness)


TRACE_ENV_KEYS = ("TRACEPARENT", "TRACESTATE")


def current_carrier() -> dict[str, str]:
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier


def extract_context(carrier: Mapping[str, str]) -> otel_context.Context:
    return TraceContextTextMapPropagator().extract(carrier, context=otel_context.Context())


def exec_trace_environment(environ: Mapping[str, str]) -> dict[str, str]:
    env = {key: value for key, value in environ.items() if key not in TRACE_ENV_KEYS}
    env.update({key.upper(): value for key, value in current_carrier().items()})
    return env


def exec_parent_context(environ: Mapping[str, str]) -> otel_context.Context:
    return extract_context({key.lower(): environ[key] for key in TRACE_ENV_KEYS if key in environ})


def inbox_span_context(carrier: Mapping[str, object] | None) -> otel_trace.SpanContext | None:
    if not carrier:
        return None
    headers = {key: value for key, value in carrier.items() if isinstance(value, str)}
    span_context = otel_trace.get_current_span(extract_context(headers)).get_span_context()
    return span_context if span_context.is_valid else None


_TURN_ROOT = otel_context.create_key("meiva.turn.root")


@contextlib.contextmanager
def turn_span(*, agent_id: int, turn: int) -> Generator[None, None, None]:
    """Trace one graph invocation with its own root and consumed-input links."""
    if not _recording_ready(PROCESS_RECORDING, timeout=TRACE_INIT_WAIT_S):
        yield
        return
    attributes = {
        _ATTR_NEUTRAL_SESSION_ID: str(agent_id),
        _ATTR_TURN: turn,
        "meiva.agent_id": agent_id,
    }
    empty = otel_context.Context()
    root = otel_trace.get_tracer("meiva.session").start_span(
        f"meiva-agent-{agent_id}", context=empty, attributes=attributes
    )
    turn_context = otel_context.set_value(
        _TURN_ROOT, root, otel_trace.set_span_in_context(root, empty)
    )
    token = otel_context.attach(turn_context)
    try:
        yield
    finally:
        otel_context.detach(token)
        root.end()


def link_inbox_input(carrier: Mapping[str, object] | None) -> None:
    """Link a claimed or reserved inbound to the active turn root."""
    root = otel_context.get_value(_TURN_ROOT)
    if isinstance(root, otel_trace.Span) and root.is_recording():
        source = inbox_span_context(carrier)
        if source is not None and source.trace_id != root.get_span_context().trace_id:
            root.add_link(source)


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
