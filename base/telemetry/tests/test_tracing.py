"""Tests for base.telemetry.tracing — local OTLP-JSON span recording (no network)."""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry import tracing as tracing_mod
from base.telemetry.otlp.trace_config import TraceConfig
from base.telemetry.otlp.trace_readiness import TraceReadiness
from base.telemetry.tests.otlp.identity_inputs import machine_identity
from base.telemetry.tracing import (
    OtlpJsonHttpSpanExporter,
    initialize_tracing,
)


# meiva:note
# Loopback HTTP collector recording OTLP trace POSTs.
class CollectorStubServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.posts: list[tuple[str, bytes, dict[str, str]]] = []
        self.status_sequence: list[int] = []
        self.block_event: threading.Event | None = None
        self._cond = threading.Condition()
        self._in_flight = 0
        owner = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                with owner._cond:
                    owner._in_flight += 1
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length) if length > 0 else b""
                    headers = dict(self.headers.items())
                    owner.posts.append((f"{owner.endpoint}{self.path}", body, headers))
                    if owner.block_event is not None:
                        owner.block_event.wait(timeout=2.0)
                    status = owner.status_sequence.pop(0) if owner.status_sequence else 200
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(b"{}")
                finally:
                    with owner._cond:
                        owner._in_flight -= 1
                        if owner._in_flight == 0:
                            owner._cond.notify_all()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer((host, port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def reset(self) -> None:
        if self.block_event is not None:
            self.block_event.set()
        with self._cond:
            self._cond.wait_for(lambda: self._in_flight == 0, timeout=2.0)
        self.posts.clear()
        self.status_sequence.clear()
        self.block_event = None

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


_shared_collector_server: CollectorStubServer | None = None
_active_collector_endpoint: str = "http://127.0.0.1:4318"


# meiva:note
# A recording config: the settings' defaults unless a test states otherwise.
def trace_config(
    *,
    enabled: bool = True,
    strip_content: bool = True,
    endpoint: str | None = None,
    retention_days: int = 3,
    max_dir_mb: int = 2048,
    disk_watermark: float = 1.0,
    endpoint_explicit: bool = False,
    retry_interval_s: float = 300.0,
    gzip_grace_seconds: int = 60,
) -> TraceConfig:
    return TraceConfig(
        enabled=enabled,
        strip_content=strip_content,
        endpoint=_active_collector_endpoint if endpoint is None else endpoint,
        retention_days=retention_days,
        max_dir_mb=max_dir_mb,
        disk_watermark=disk_watermark,
        endpoint_explicit=endpoint_explicit,
        retry_interval_s=retry_interval_s,
        gzip_grace_s=gzip_grace_seconds,
    )


_TRACE_BG_THREAD_NAMES = frozenset({"trace-arm", "trace-collector-retry"})


def _assert_no_background_trace_threads() -> None:
    """A background trace thread from a previous test must not be in flight —
    a zombie arm finishing inside the next test runs whatever Traceloop.init
    that test has installed (the #1065 delta attempt 1 flake: a second call
    counted in test_idempotent_second_call_is_noop's lambda)."""
    leftovers = [
        t.name for t in threading.enumerate() if t.name in _TRACE_BG_THREAD_NAMES and t.is_alive()
    ]
    assert not leftovers, f"previous test leaked background thread(s): {leftovers}"


_THREAD_DRAIN_TIMEOUT_S = 5.0


def _drain_background_trace_threads(*, trace_readiness: TraceReadiness) -> None:
    assert tracing_mod.shutdown(timeout=_THREAD_DRAIN_TIMEOUT_S, readiness=trace_readiness) == ()
    for key in ("retry_owner", "arm_owner"):
        owner = tracing_mod.state.get(key)
        if owner is not None:
            assert owner.finished.is_set(), "the trace owner escaped its finite drain"
    _assert_no_background_trace_threads()


@pytest.fixture
def trace_readiness() -> TraceReadiness:
    return TraceReadiness()


@pytest.fixture(autouse=True)
def reset_trace_workers(trace_readiness: TraceReadiness) -> Iterator[None]:
    _assert_no_background_trace_threads()
    tracing_mod.reset_tracing_state()
    yield
    _drain_background_trace_threads(trace_readiness=trace_readiness)
    tracing_mod.reset_tracing_state()


def wait_init_resolved(timeout: float = 5.0, *, trace_readiness: TraceReadiness) -> None:
    owner = tracing_mod.state.get("arm_owner")
    if owner is not None:
        assert owner.finished.wait(timeout=timeout), "background trace arm did not finish"
    assert trace_readiness.resolved, "background trace attempt did not resolve"


# meiva:note
# Share a loopback HTTP collector across tests, resetting its state per test.
@pytest.fixture(autouse=True)
def collector_up() -> Iterator[CollectorStubServer]:
    global _active_collector_endpoint, _shared_collector_server  # noqa: PLW0603
    if _shared_collector_server is None:
        _shared_collector_server = CollectorStubServer()
    _shared_collector_server.reset()
    previous = _active_collector_endpoint
    _active_collector_endpoint = _shared_collector_server.endpoint
    try:
        yield _shared_collector_server
    finally:
        _active_collector_endpoint = previous
        _shared_collector_server.reset()


def test_disabled_returns_early(tmp_path: Path, *, trace_readiness: TraceReadiness) -> None:
    """When trace_enabled=False, initialize_tracing returns None and does not init the SDK."""
    home = tmp_path / ".meiva"
    home.mkdir()
    assert (
        initialize_tracing(
            machine=machine_identity(),
            home=home,
            config=trace_config(enabled=False),
            readiness=trace_readiness,
        )
        is None
    )
    assert trace_readiness.ready is False


def warned_fields(records: LogRecords) -> list[dict[str, Any]]:
    """The structured fields of every WARNING logged so far, in order (the tracing module tags
    its warnings with `action=` and the details of the episode)."""
    return [r["extra"] for r in records if r["level"].name == "WARNING"]


def test_enabled_inits_traceloop_with_otlp_exporter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    collector_up: CollectorStubServer,
    *,
    trace_readiness: TraceReadiness,
) -> None:
    """trace_enabled=True: Traceloop.init is handed an OtlpJsonHttpSpanExporter
    pointed at the LOCAL collector as the sole exporter (no api_endpoint/api_key
    network sink), batch on, traceloop's own telemetry off, plus the instruments
    set covering Anthropic/OpenAI/LangChain/Google."""
    home = tmp_path / ".meiva-test"
    home.mkdir()

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(
            enabled=True,
            endpoint=collector_up.endpoint,
            endpoint_explicit=True,
        ),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert len(calls) == 1
    kw = calls[0]
    exporter = kw["exporter"]
    assert isinstance(exporter, OtlpJsonHttpSpanExporter)
    assert exporter._endpoint == f"{collector_up.endpoint}/v1/traces"
    assert "api_endpoint" not in kw  # no SDK-level network sink; the exporter owns the POST
    assert "api_key" not in kw
    assert kw["telemetry_enabled"] is False
    assert kw["disable_batch"] is False
    assert kw["resource_attributes"] == {
        "cluster": ".meiva-test",
        "service.line": "meiva",
        "environment": "dev",
    }

    from traceloop.sdk.instruments import Instruments

    instruments = kw["instruments"]
    assert Instruments.ANTHROPIC in instruments
    assert Instruments.OPENAI in instruments
    assert Instruments.LANGCHAIN in instruments
    assert Instruments.GOOGLE_GENERATIVEAI in instruments

    assert trace_readiness.ready is True


def test_gateway_trace_recording_skips_without_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()

    calls: list[dict[str, object]] = []

    def record_init(**kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", record_init)

    machine = machine_identity(name="test-gateway", roles=frozenset({"gateway"}))
    initialize_tracing(
        machine=machine, home=home, config=trace_config(enabled=True), readiness=trace_readiness
    )

    assert calls == []
    assert trace_readiness.ready is False


def test_gateway_trace_recording_arms_with_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()
    (home / "lgtm-host").touch()

    calls: list[dict[str, object]] = []

    def record_init(**kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", record_init)

    machine = machine_identity(name="test-gateway", roles=frozenset({"gateway"}))
    initialize_tracing(
        machine=machine, home=home, config=trace_config(enabled=True), readiness=trace_readiness
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert len(calls) == 1
    assert calls[0]["resource_attributes"] == {
        "cluster": ".meiva",
        "service.line": "meiva",
        "environment": "prod",
    }


def test_sdk_initialize_failure_logs_and_state_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: LogRecords,
    *,
    trace_readiness: TraceReadiness,
) -> None:
    # meiva:note
    # If Traceloop.init() itself raises on the arm thread, the failure is
    # logged (not propagated — tracing is observability, not a boot blocker),
    # readiness remains false, and the wait resolves so turns still run.
    home = tmp_path / ".meiva"
    home.mkdir()

    def _raise(**_kw: object) -> None:
        raise RuntimeError("boom: init failed")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _raise)
    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )  # must not raise: the failure is the arm thread's
    wait_init_resolved(trace_readiness=trace_readiness)

    assert trace_readiness.ready is False
    assert trace_readiness.resolved and not trace_readiness.ready
    warned = warned_fields(loguru_records)
    assert warned
    assert warned[0].get("action") == "recording_init_failed"


def test_arm_failure_blocks_rearming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    """One arm attempt per process: after a failed init, a later call (e.g. a
    collector-retry re-entry) must NOT run Traceloop.init again — the
    TracerWrapper singleton would fake-succeed without the instrumentors."""
    home = tmp_path / ".meiva"
    home.mkdir()

    calls: list[int] = []

    def _fail_then_record(**_kw: object) -> None:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _fail_then_record)

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)
    assert trace_readiness.resolved and not trace_readiness.ready

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )  # must be a no-op, not a second arming attempt
    assert calls == [1]
    assert trace_readiness.ready is False


def test_idempotent_second_call_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    """Second call within the same process does not re-initialize."""
    home = tmp_path / ".meiva"
    home.mkdir()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert len(calls) == 1


def test_collector_unreachable_retries_once_until_init_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: LogRecords,
    *,
    trace_readiness: TraceReadiness,
) -> None:
    """One daemon loop retries a collector-unreachable preflight, logs the
    episode once, and exits after tracing initializes."""
    home = tmp_path / ".meiva"
    home.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    cfg = trace_config(enabled=True, endpoint=endpoint, retry_interval_s=0.05)

    initialized = threading.Event()

    def init_traceloop(**_kwargs: object) -> None:
        initialized.set()

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", init_traceloop)

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=cfg,
        readiness=trace_readiness,
    )
    retry_thread = tracing_mod.state["retry_owner"]._thread
    assert isinstance(retry_thread, threading.Thread)

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=cfg,
        readiness=trace_readiness,
    )
    assert tracing_mod.state["retry_owner"]._thread is retry_thread

    late_server = CollectorStubServer(port=port)
    try:
        assert initialized.wait(timeout=2.0)
        retry_thread.join(timeout=1.0)
        wait_init_resolved(trace_readiness=trace_readiness)
    finally:
        late_server.close()

    assert loguru_records.messages("WARNING") == [
        "trace recording disabled — local OTel collector not answering"
    ]
    assert trace_readiness.ready is True
    assert tracing_mod.state["collector_offline_reported"] is False
    assert not retry_thread.is_alive()


def test_arming_runs_off_the_caller_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    # meiva:note
    # The heavy part never blocks the boot path: initialize_tracing returns
    # while Traceloop.init is still pending (the mock blocks until released),
    # and shared recording readiness is what use sites wait on.
    home = tmp_path / ".meiva"
    home.mkdir()

    entered = threading.Event()
    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        entered.set()
        assert release.wait(timeout=5.0), "test released the arm thread"

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )  # returns immediately — the mock is still blocked
    assert entered.wait(timeout=5.0), "arm thread must have started"
    assert trace_readiness.ready is False  # boot already returned; init pending

    release.set()
    wait_init_resolved(trace_readiness=trace_readiness)
    assert trace_readiness.ready is True


def test_teardown_drains_pending_arm_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    """A test body that returns while the arm thread is still in flight must
    not leave a zombie behind: its delayed Traceloop.init would then land in
    the NEXT test's monkeypatched lambda (counted as a second init — the
    #1065 delta attempt 1 flake). This test only has to leave one in flight;
    the reset_tracing_init_flag teardown has to drain it, and the setup boundary
    check turns a leftover into a deterministic failure."""
    home = tmp_path / ".meiva"
    home.mkdir()

    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        release.wait(timeout=1.0)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    arm_thread = tracing_mod.state["arm_owner"]._thread
    assert isinstance(arm_thread, threading.Thread)
    assert arm_thread.is_alive()
    # Intentionally no wait_init_resolved(): the fixture teardown must drain.


def test_arm_tracing_skips_init_when_already_resolved(tmp_path: Path) -> None:
    from base.telemetry.tests.capture.trace_process import run_trace_scenario

    run_trace_scenario(tmp_path, "resolved")


def test_concurrent_arm_threads_init_once(tmp_path: Path) -> None:
    from base.telemetry.tests.capture.trace_process import run_trace_scenario

    run_trace_scenario(tmp_path, "concurrent", lifecycle=True)


def test_turn_span_waits_for_pending_arm(tmp_path: Path) -> None:
    from base.telemetry.tests.capture.trace_process import run_trace_scenario

    run_trace_scenario(tmp_path, "first-turn", lifecycle=True)


def test_shared_recording_read_has_a_bounded_wait(trace_readiness: TraceReadiness) -> None:
    assert trace_readiness.reserve()
    first = trace_readiness.read(timeout=0.01)
    assert not first.ready and first.first_timeout
    assert not trace_readiness.read(timeout=0).first_timeout
    assert not trace_readiness.resolved
    trace_readiness.close()


# --- OtlpJsonHttpSpanExporter ---------------------------------------------------


# --- turn_span placeholder-root export timing (#1964) ------------------------------


def spans_received(posts: list[tuple[str, bytes, dict[str, str]]]) -> list[Any]:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    requests = [ExportTraceServiceRequest.FromString(raw) for _url, raw, _h in posts]
    return [
        span
        for req in requests
        for rs in req.resource_spans
        for ss in rs.scope_spans
        for span in ss.spans
    ]


def assert_turn_root_exported_at_start(spans: list[Any]) -> Any:
    assert len(spans) == 1, f"root must export at turn start, got {len(spans)}"
    root = spans[0]
    assert root.name == "meiva-agent-7"
    attrs = {kv.key: kv.value.string_value or kv.value.int_value for kv in root.attributes}
    assert attrs["session.id"] == "7"
    assert attrs["meiva.turn"] == 3
    assert root.end_time_unix_nano > 0, "placeholder root must be ended when exported"
    return root


def assert_root_once_with_child_parented_under_it(spans: list[Any], root: Any) -> None:
    roots = [s for s in spans if not s.parent_span_id]
    assert len(roots) == 1, f"root must be exported exactly once, got {len(roots)}"
    assert roots[0].span_id == root.span_id
    children = [s for s in spans if s.parent_span_id]
    assert len(children) == 1
    assert children[0].parent_span_id == root.span_id
    assert children[0].trace_id == root.trace_id


# --- retention prune ---------------------------------------------------------


# --- turn_span -----------------------------------------------------------


# --- claim idle-wait span -------------------------------------------------


# --- trace v2: content stripping --------------------------------------------


def otlp_with_attrs(attrs: dict[str, str]) -> dict:
    """Build a minimal OTLP export request with one span carrying the given
    attributes (keys -> string values)."""
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "execute_task after_init",
                                "attributes": [
                                    {"key": k, "value": {"stringValue": v}}
                                    for k, v in attrs.items()
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


# --- trace v2: file governance -----------------------------------------------
