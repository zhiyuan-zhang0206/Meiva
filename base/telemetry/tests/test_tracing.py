"""Tests for base.telemetry.tracing — local OTLP-JSON span recording (no network)."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base import config
from base.cluster import derive
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry import tracing as tracing_mod
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.otlp.trace_config import TraceConfig
from base.telemetry.otlp.trace_readiness import TraceReadiness
from base.telemetry.tests.otlp.identity_inputs import (
    allow_identity,
    deny_identity,
    machine_identity,
)
from base.telemetry.tracing import (
    OtlpJsonHttpSpanExporter,
    initialize_tracing,
)


@pytest.fixture(autouse=True)
def production_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_otlp, "production_identity", allow_identity, raising=False)


# meiva:note
# A recording config: the settings' defaults unless a test states otherwise.
def trace_config(
    *,
    enabled: bool = True,
    strip_content: bool = True,
    endpoint: str = "http://127.0.0.1:4318",
    retention_days: int = 3,
    max_dir_mb: int = 2048,
    disk_watermark: float = 0.9,
    endpoint_explicit: bool = False,
) -> TraceConfig:
    return TraceConfig(
        enabled=enabled,
        strip_content=strip_content,
        endpoint=endpoint,
        retention_days=retention_days,
        max_dir_mb=max_dir_mb,
        disk_watermark=disk_watermark,
        endpoint_explicit=endpoint_explicit,
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
def reset_trace_workers(monkeypatch: pytest.MonkeyPatch, trace_readiness: TraceReadiness):
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "")
    _assert_no_background_trace_threads()
    tracing_mod.state.clear()
    tracing_mod.state.update(
        closed=False, collector_offline_reported=False, retry_owner=None, arm_owner=None
    )
    yield
    _drain_background_trace_threads(trace_readiness=trace_readiness)
    tracing_mod.state.clear()
    tracing_mod.state.update(
        closed=False, collector_offline_reported=False, retry_owner=None, arm_owner=None
    )


def wait_init_resolved(timeout: float = 5.0, *, trace_readiness: TraceReadiness) -> None:
    owner = tracing_mod.state.get("arm_owner")
    if owner is not None:
        assert owner.finished.wait(timeout=timeout), "background trace arm did not finish"
    assert trace_readiness.resolved, "background trace attempt did not resolve"


@pytest.fixture(autouse=True)
def collector_up(monkeypatch: pytest.MonkeyPatch):
    """The local-collector preflight must pass by default — the tests exercise
    the exporter/init logic, not the network probe (which is covered by its own
    telemetry_otlp tests)."""
    monkeypatch.setattr(
        tracing_mod,
        "endpoint_reachable",
        lambda _e: True,  # pyright: ignore[reportUnknownArgumentType]
    )


def test_disabled_returns_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
):
    """When trace_enabled=False, initialize_tracing returns None and does not init the SDK."""
    assert (
        initialize_tracing(
            machine=machine_identity(),
            home=tmp_path,
            config=trace_config(enabled=False),
            readiness=trace_readiness,
        )
        is None
    )
    assert trace_readiness.ready is False


def fixed_disk_usage(fraction: float, free: int) -> Callable[..., tuple[float, int]]:
    """A `disk_usage` stand-in that reports one reading whatever directory it is asked about."""

    def disk_usage(*, traces_dir: Path) -> tuple[float, int]:
        _ = traces_dir
        return fraction, free

    return disk_usage


def warned_fields(records: LogRecords) -> list[dict[str, Any]]:
    """The structured fields of every WARNING logged so far, in order (the tracing module tags
    its warnings with `action=` and the details of the episode)."""
    return [r["extra"] for r in records if r["level"].name == "WARNING"]


def under_watermark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk usage below the watermark: initialize_tracing's auto-degrade guard
    must not skip recording in tests (the dev disk can be >90% full and would
    otherwise make every init-path test environment-dependent)."""
    monkeypatch.setattr(tracing_mod, "disk_usage", fixed_disk_usage(0.1, 100 * 1024**3))


def test_enabled_inits_traceloop_with_otlp_exporter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
):
    """trace_enabled=True: Traceloop.init is handed an OtlpJsonHttpSpanExporter
    pointed at the LOCAL collector as the sole exporter (no api_endpoint/api_key
    network sink), batch on, traceloop's own telemetry off, plus the instruments
    set covering Anthropic/OpenAI/LangChain/Google."""
    monkeypatch.setattr(
        config.settings.observability,
        "telemetry_otlp_endpoint",
        "http://127.0.0.1:4318",
    )

    def label(_home: Path | None = None) -> str:
        return ".meiva-test"

    monkeypatch.setattr(tracing_mod, "cluster_label", label)
    monkeypatch.setattr(derive, "production_identity", deny_identity)
    under_watermark(monkeypatch)

    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]
    kw = calls[0]
    exporter = kw["exporter"]
    assert isinstance(exporter, OtlpJsonHttpSpanExporter)
    assert exporter._endpoint == "http://127.0.0.1:4318/v1/traces"
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
    home = tmp_path / ".meiva-preview"
    home.mkdir()
    monkeypatch.setattr(telemetry_otlp, "production_identity", allow_identity)
    monkeypatch.delitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", raising=False)

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
    monkeypatch.setattr(telemetry_otlp, "production_identity", allow_identity)
    monkeypatch.setattr(derive, "production_identity", allow_identity)
    monkeypatch.delitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", raising=False)

    under_watermark(monkeypatch)
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
):
    # meiva:note
    # If Traceloop.init() itself raises on the arm thread, the failure is
    # logged (not propagated — tracing is observability, not a boot blocker),
    # readiness remains false, and the wait resolves so turns still run.
    under_watermark(monkeypatch)

    def _raise(**_kw):
        raise RuntimeError("boom: init failed")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _raise)  # pyright: ignore[reportUnknownArgumentType]
    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
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
):
    """One arm attempt per process: after a failed init, a later call (e.g. a
    collector-retry re-entry) must NOT run Traceloop.init again — the
    TracerWrapper singleton would fake-succeed without the instrumentors."""
    under_watermark(monkeypatch)

    calls: list[int] = []

    def _fail_then_record(**_kw: object) -> None:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _fail_then_record)

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)
    assert trace_readiness.resolved and not trace_readiness.ready

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )  # must be a no-op, not a second arming attempt
    assert calls == [1]
    assert trace_readiness.ready is False


def test_idempotent_second_call_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
):
    """Second call within the same process does not re-initialize."""
    under_watermark(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]


def test_collector_unreachable_retries_once_until_init_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: LogRecords,
    *,
    trace_readiness: TraceReadiness,
) -> None:
    """One daemon loop retries a collector-unreachable preflight, logs the
    episode once, and exits after tracing initializes."""
    monkeypatch.setattr(tracing_mod, "COLLECTOR_RETRY_INTERVAL_S", 0.1)
    under_watermark(monkeypatch)

    reachable = iter((False, False, True))
    attempts: list[bool] = []

    def endpoint_reachable(_endpoint: str) -> bool:
        result = next(reachable)
        attempts.append(result)
        return result

    monkeypatch.setattr(tracing_mod, "endpoint_reachable", endpoint_reachable)
    initialized = threading.Event()

    def init_traceloop(**_kwargs: object) -> None:
        initialized.set()

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", init_traceloop)

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    retry_thread = tracing_mod.state["retry_owner"]._thread
    assert isinstance(retry_thread, threading.Thread)

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    assert tracing_mod.state["retry_owner"]._thread is retry_thread
    assert initialized.wait(timeout=1.0)
    retry_thread.join(timeout=0.5)
    wait_init_resolved(trace_readiness=trace_readiness)

    assert attempts == [False, False, True]
    assert loguru_records.messages("WARNING") == [
        "trace recording disabled — local OTel collector not answering"
    ]
    assert trace_readiness.ready is True
    assert tracing_mod.state["collector_offline_reported"] is False
    assert not retry_thread.is_alive()


def test_arming_runs_off_the_caller_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
):
    # meiva:note
    # The heavy part never blocks the boot path: initialize_tracing returns
    # while Traceloop.init is still pending (the mock blocks until released),
    # and shared recording readiness is what use sites wait on.
    under_watermark(monkeypatch)

    entered = threading.Event()
    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        entered.set()
        assert release.wait(timeout=5.0), "test released the arm thread"

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
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
):
    """A test body that returns while the arm thread is still in flight must
    not leave a zombie behind: its delayed Traceloop.init would then land in
    the NEXT test's monkeypatched lambda (counted as a second init — the
    #1065 delta attempt 1 flake). This test only has to leave one in flight;
    the reset_tracing_init_flag teardown has to drain it, and the setup boundary
    check turns a leftover into a deterministic failure."""
    under_watermark(monkeypatch)

    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        release.wait(timeout=1.0)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing(
        machine=machine_identity(),
        home=tmp_path,
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


def capture_otlp_json_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes, dict]]:
    """Route `httpx.post` into a list so exported OTLP/JSON batches can be decoded."""
    posts: list[tuple[str, bytes, dict]] = []

    def _post(url, *, content, headers, timeout):
        posts.append((url, content, headers))  # pyright: ignore[reportUnknownMemberType]
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("httpx.post", _post)  # pyright: ignore[reportUnknownArgumentType]
    return posts


def spans_received(posts: list[tuple[str, bytes, dict]]) -> list[Any]:
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
