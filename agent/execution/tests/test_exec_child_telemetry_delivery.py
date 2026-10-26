from __future__ import annotations

import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent.execution.protocol import (
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)
from base.agents.context.clients import ClientSet
from base.native_process.tests.host_pipe import live_host_control
from meiva.tests.fixtures.pin_agent import exec_context

_AGENT_ID = 424242


_POSTS: list[tuple[str, bytes]] = []
_POSTS_LOCK = threading.Lock()


class _ReceiverHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        with _POSTS_LOCK:
            _POSTS.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def otlp_receiver() -> Any:
    _POSTS.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ReceiverHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _endpoint(server: Any) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _spawn(
    tmp_path: Path, code: str, endpoint: str, *, corrupt_request: bool = False
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


def _mirror_records(tmp_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((tmp_path / "home" / "logs").glob("events-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def _mirror_keys(tmp_path: Path) -> set[tuple[str, object, object]]:
    keys: set[tuple[str, object, object]] = set()
    for record in _mirror_records(tmp_path):
        attributes = cast("dict[str, Any]", record.get("attributes") or {})
        keys.add((str(record.get("event_name")), attributes.get("envelope"), attributes.get("op")))
    return keys


def _otlp_attributes(record: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for kv in record.attributes:
        if kv.value.HasField("string_value"):
            attrs[kv.key] = kv.value.string_value
        elif kv.value.HasField("int_value"):
            attrs[kv.key] = kv.value.int_value
    return attrs


def _record_key(record: Any) -> tuple[str, object, object]:
    attrs = _otlp_attributes(record)
    payload: dict[str, Any] = {}
    if record.body.string_value:
        with contextlib.suppress(Exception):
            payload = json.loads(record.body.string_value).get("attributes") or {}
    return (str(attrs.get("event_name")), payload.get("envelope"), payload.get("op"))


def _batch_keys(raw: bytes) -> set[tuple[str, object, object]]:
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    request = logs_service_pb2.ExportLogsServiceRequest()
    with contextlib.suppress(Exception):
        request.ParseFromString(raw)
    return {
        _record_key(record)
        for resource_logs in request.resource_logs
        for scope_logs in resource_logs.scope_logs
        for record in scope_logs.log_records
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


def test_done_child_delivers_result_write_record(tmp_path: Path, otlp_receiver: Any) -> None:
    proc, result_path = _spawn(tmp_path, "print('delivery ran')", _endpoint(otlp_receiver))
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

    mirror_keys = _mirror_keys(tmp_path)
    assert ("exec_envelope", "result", "write") in mirror_keys, f"mirror saw: {sorted(mirror_keys)}"


def test_crashed_child_delivers_its_records(tmp_path: Path, otlp_receiver: Any) -> None:
    proc, result_path = _spawn(
        tmp_path, "raise ValueError('delivery crash')", _endpoint(otlp_receiver)
    )
    assert proc.returncode == 0, proc.stderr
    assert read_result(result_path).kind == "crashed"

    keys = _poll_sent({("exec_envelope", "result", "write")})
    assert ("exec_envelope", "result", "write") in keys, f"receiver saw: {sorted(keys)}"


def test_boot_crash_child_delivers_the_crash_envelope_record(
    tmp_path: Path, otlp_receiver: Any
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

    pipe = EventPipeline(writer=writer, batch_size=1)
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
