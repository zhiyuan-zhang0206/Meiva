from __future__ import annotations

import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Mapping
from pathlib import Path
from uuid import UUID

import pytest

from agent.execution.protocol import (
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)
from base.agents.context.clients import ClientSet
from base.clock.tests.fakes import system_clock
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.native_process.tests.host_pipe import live_host_control
from meiva.tests.fixtures.pin_agent import exec_context

_AGENT_ID = 424242
_TRACE_DRAIN_PROBE = Path(__file__).with_name("_exec_trace_drain_child.py")


_POSTS: list[tuple[str, bytes]] = []
_POSTS_LOCK = threading.Lock()


class _ReceiverServer(http.server.ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ReceiverHandler)
        self.trace_requested = threading.Event()
        self.trace_response_allowed = threading.Event()
        self.trace_response_allowed.set()


class _ReceiverHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        with _POSTS_LOCK:
            _POSTS.append((self.path, body))
        if self.path == "/v1/traces" and isinstance(server := self.server, _ReceiverServer):
            server.trace_requested.set()
            _ = server.trace_response_allowed.wait(10)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def otlp_receiver() -> Generator[_ReceiverServer]:
    _POSTS.clear()
    server = _ReceiverServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _endpoint(server: http.server.ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def _spawn(
    tmp_path: Path,
    code: str,
    endpoint: str,
    *,
    corrupt_request: bool = False,
    trace_environment: Mapping[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=_AGENT_ID, request_id=UUID(int=1))
    result_path = make_result_path(exec_dir, agent_id=_AGENT_ID, result_id=UUID(int=2))
    if corrupt_request:
        request_path.write_text("corrupt request body", encoding="utf-8")
    else:
        write_request(
            request_path,
            code=code,
            context=exec_context(_AGENT_ID).describe(),
            timeout_s=60.0,
            state=None,
            incarnation=None,
        )
    env = os.environ.copy()
    env.update(
        {
            "MEIVA_HOME": str(tmp_path / "home"),
            "MEIVA_AGENT_ID": str(_AGENT_ID),
            "MEIVA_PROCESS_PROFILE": "agent",
            "MEIVA_EXEC_REQUEST_FILE": str(request_path),
            "MEIVA_EXEC_RESULT_FILE": str(result_path),
            "MEIVA_TELEMETRY_OTLP_ENDPOINT": endpoint,
            "MEIVA_TELEMETRY_OTLP_ENABLED": "true",
            **(trace_environment or {}),
            "MEIVA_EXEC_WATCHDOG_MARGIN_S": "0.5",
        }
    )
    env.pop("MEIVA_LOG_DIR", None)
    with live_host_control() as stdin:
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-m", "agent.execution.child"],
            stdin=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
    return proc, result_path


def _attributes_of(record: dict[str, object]) -> dict[str, object]:
    attributes = record.get("attributes")
    if attributes is None:
        return {}
    assert is_object_dict(attributes), attributes
    return attributes


def _children(node: object, key: str) -> list[object]:
    if not is_object_dict(node):
        return []
    items = node.get(key)
    return items if is_object_list(items) else []


def _otlp_attributes(record: object) -> dict[str, object]:
    attrs: dict[str, object] = {}
    for kv in _children(record, "attributes"):
        assert is_object_dict(kv), kv
        value = kv.get("value")
        assert is_object_dict(value), value
        key = kv.get("key")
        assert isinstance(key, str), key
        if "string_value" in value:
            attrs[key] = value["string_value"]
        elif "int_value" in value:
            int_value = value["int_value"]
            assert isinstance(int_value, (int, str)), int_value
            attrs[key] = int(int_value)
    return attrs


def _record_key(record: object) -> tuple[str, object, object]:
    attrs = _otlp_attributes(record)
    payload: dict[str, object] = {}
    body = record.get("body") if is_object_dict(record) else None
    body_text = body.get("string_value") if is_object_dict(body) else None
    if body_text and isinstance(body_text, str):
        with contextlib.suppress(Exception):
            decoded = object_of(json.loads, body_text)
            if is_object_dict(decoded):
                payload = _attributes_of(decoded)
    return (str(attrs.get("event_name")), payload.get("envelope"), payload.get("op"))


def _batch_keys(raw: bytes) -> set[tuple[str, object, object]]:
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    request = logs_service_pb2.ExportLogsServiceRequest()
    with contextlib.suppress(Exception):
        request.ParseFromString(raw)
    document = object_of(MessageToDict, request, preserving_proto_field_name=True)
    return {
        _record_key(record)
        for resource_logs in _children(document, "resource_logs")
        for scope_logs in _children(resource_logs, "scope_logs")
        for record in _children(scope_logs, "log_records")
    }


def _sent_keys() -> set[tuple[str, object, object]]:
    keys: set[tuple[str, object, object]] = set()
    with _POSTS_LOCK:
        posts = list(_POSTS)
    for path, raw in posts:
        if not path.endswith("/v1/logs") or len(raw) < 4:
            continue
        keys |= _batch_keys(raw)
    return keys


def _poll_sent(expected: set[tuple[str, object, object]]) -> set[tuple[str, object, object]]:
    deadline = time.monotonic() + 10.0
    while True:
        keys = _sent_keys()
        if expected <= keys or time.monotonic() > deadline:
            return keys
        time.sleep(0.05)


def test_done_child_delivers_result_write_record(
    tmp_path: Path, otlp_receiver: http.server.ThreadingHTTPServer
) -> None:
    trace_id = "102030405060708090a0b0c0d0e0f000"
    parent_id = "face0000feed0000"
    code = """
from opentelemetry import trace

active = trace.get_current_span()
assert active.parent is not None
assert active.parent.span_id == 0xface0000feed0000
with trace.get_tracer("exec-test").start_as_current_span("correlation-probe"):
    print("delivery ran")
assert trace.get_tracer_provider().force_flush()
"""
    proc, result_path = _spawn(
        tmp_path,
        code,
        _endpoint(otlp_receiver),
        trace_environment={"TRACEPARENT": f"00-{trace_id}-{parent_id}-01"},
    )
    assert proc.returncode == 0, proc.stderr
    assert read_result(result_path).kind == "done"

    keys = _poll_sent(
        {
            ("exec_child_boot", None, None),
            ("exec_envelope", "request", "read"),
            ("exec_envelope", "result", "write"),
        }
    )
    assert ("exec_child_boot", None, None) in keys, f"receiver saw: {sorted(keys)}"
    assert ("exec_envelope", "request", "read") in keys, f"receiver saw: {sorted(keys)}"
    assert ("exec_envelope", "result", "write") in keys, f"receiver saw: {sorted(keys)}"

    _assert_child_correlation(trace_id)


def _posted_spans() -> list[tuple[str, str, str]]:
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    with _POSTS_LOCK:
        posts = list(_POSTS)
    return [
        (span.name, span.trace_id.hex(), span.parent_span_id.hex())
        for path, raw in posts
        if path == "/v1/traces"
        for resource in trace_service_pb2.ExportTraceServiceRequest.FromString(raw).resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]


def _posted_correlations() -> set[tuple[str, str]]:
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    with _POSTS_LOCK:
        posts = list(_POSTS)
    return {
        (record.trace_id.hex(), record.span_id.hex())
        for path, raw in posts
        if path.endswith("/v1/logs") and len(raw) >= 4
        for resource in logs_service_pb2.ExportLogsServiceRequest.FromString(raw).resource_logs
        for scope in resource.scope_logs
        for record in scope.log_records
        if _record_key(MessageToDict(record, preserving_proto_field_name=True))[0]
        in {"exec_child_boot", "exec_envelope"}
    }


def _assert_child_correlation(trace_id: str) -> None:
    [(_, probe_trace_id, probe_parent)] = [
        span for span in _posted_spans() if span[0] == "correlation-probe"
    ]
    assert probe_trace_id == trace_id
    assert _posted_correlations() == {(trace_id, probe_parent)}


def test_exec_trace_drain_exits_while_collector_response_is_blocked(
    tmp_path: Path, otlp_receiver: _ReceiverServer
) -> None:
    drain = tmp_path / "begin-drain"
    otlp_receiver.trace_response_allowed.clear()
    with subprocess.Popen(  # noqa: S603 — fixed interpreter and test-owned probe
        [sys.executable, "-I", str(_TRACE_DRAIN_PROBE), _endpoint(otlp_receiver), str(drain)]
    ) as process:
        try:
            assert otlp_receiver.trace_requested.wait(15)
            drain.touch()
            assert process.wait(timeout=1) == 0
        finally:
            drain.touch()
            otlp_receiver.trace_response_allowed.set()


def test_crashed_child_delivers_its_records(
    tmp_path: Path, otlp_receiver: http.server.ThreadingHTTPServer
) -> None:
    proc, result_path = _spawn(
        tmp_path, "raise ValueError('delivery crash')", _endpoint(otlp_receiver)
    )
    assert proc.returncode == 0, proc.stderr
    assert read_result(result_path).kind == "crashed"

    keys = _poll_sent({("exec_envelope", "result", "write")})
    assert ("exec_envelope", "result", "write") in keys, f"receiver saw: {sorted(keys)}"


def test_boot_crash_child_delivers_the_crash_envelope_record(
    tmp_path: Path, otlp_receiver: http.server.ThreadingHTTPServer
) -> None:
    proc, result_path = _spawn(
        tmp_path, "print('never runs')", _endpoint(otlp_receiver), corrupt_request=True
    )
    assert proc.returncode == 0, proc.stderr
    payload = read_result(result_path)
    assert payload.kind == "crashed"
    assert payload.code_reached is False

    keys = _poll_sent({("exec_envelope", "result", "write")})
    assert ("exec_envelope", "result", "write") in keys, f"receiver saw: {sorted(keys)}"


# meiva:note
# The child's owned clients, whose event pipeline has a writer that fails on the first record.
# The terminal worker failure is raised again by every later drain of that pipeline, which is
# what a delivery failure looks like to the child.
@contextlib.contextmanager
def _owned_writer_that_fails(original: ValueError) -> Generator[ClientSet]:
    from datetime import UTC, datetime

    from base.telemetry import Event, EventPipeline

    def writer(batch: list[Event]) -> None:
        raise original

    pipe = EventPipeline(now=system_clock().now, writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)
    try:
        clients.event_pipeline().enqueue(
            Event(
                ts=datetime.now(UTC),
                trace_id=None,
                span_id=None,
                agent_id=None,
                machine="test",
                cluster="test",
                process="test",
                category="telemetry",
                event_name="sdk_call",
                level="info",
                source="system",
                target_agent_id=None,
            )
        )
        yield clients
        with pytest.raises(ValueError) as observed:
            clients.close(pipeline_timeout=1)
        assert observed.value is original
    finally:
        with pytest.raises(ValueError):
            pipe.stop(timeout=1)


def test_owned_writer_failure_rewrites_success_before_child_exit(tmp_path: Path) -> None:
    from agent.execution import child
    from agent.execution.protocol import ResultPayload, read_result, write_result

    original = ValueError("owned writer failed after the SDK body completed")
    result_path = tmp_path / "result.json"
    payload = ResultPayload(kind="done", code_reached=True)
    write_result(result_path, payload)
    with _owned_writer_that_fails(original) as clients:
        child.deliver_run_telemetry(str(result_path), payload, clients=clients)
        result = read_result(result_path)
        assert result.kind == "crashed" and result.code_reached is True
        assert result.exc_type == "ValueError" and result.exc_msg == str(original)


def test_owned_writer_failure_never_replaces_the_crash_the_child_reported(tmp_path: Path) -> None:
    from agent.execution import child
    from agent.execution.protocol import ResultPayload, write_result

    result_path = tmp_path / "result.json"
    payload = ResultPayload(
        kind="crashed", code_reached=True, exc_type="OriginalError", exc_msg="primary"
    )
    write_result(result_path, payload)
    before = result_path.read_bytes()
    with _owned_writer_that_fails(ValueError("owned writer failed after the crash")) as clients:
        child.deliver_run_telemetry(str(result_path), payload, clients=clients)
        assert result_path.read_bytes() == before


def test_a_failed_delivery_after_a_crash_envelope_never_reaches_the_caller() -> None:
    from agent.execution import child

    with _owned_writer_that_fails(
        ValueError("owned writer failed behind the crash envelope")
    ) as clients:
        child.deliver_envelope_telemetry(clients=clients)
