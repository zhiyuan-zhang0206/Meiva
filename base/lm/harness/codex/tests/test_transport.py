from __future__ import annotations

import asyncio
import http.server
import sys
from pathlib import Path
from threading import Event, Thread

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span

from base import telemetry
from base.clock.tests.fakes import system_clock
from base.lm.harness.codex.frames import JsonObject, JsonRpcError, TransportClosedError, as_object
from base.lm.harness.codex.transport import MAX_FRAME_BYTES, StdioAppServer
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry.otlp.telemetry_otlp import OtlpBackend
from base.telemetry.process_logging import add_event_sink, release_event_sink

SERVER = Path(__file__).with_name("fake_stdio_server.py")


def transport(
    tmp_path: Path, *, timeout: float = 5.0, max_frame_bytes: int | None = None
) -> StdioAppServer:
    return StdioAppServer(
        [sys.executable, str(SERVER)],
        cwd=tmp_path,
        environ={},
        request_timeout=timeout,
        stderr=asyncio.subprocess.DEVNULL,
        max_frame_bytes=max_frame_bytes if max_frame_bytes is not None else MAX_FRAME_BYTES,
    )


async def test_request_reply_is_correlated_by_id(tmp_path: Path) -> None:
    server = transport(tmp_path)
    await server.start()
    try:
        assert await server.request("initialize", {}) == {"userAgent": "fake-stdio"}
        assert await server.request("echo", {"x": 1}) == {"echo": "echo"}
    finally:
        await server.close()


async def test_stderr_drains_oversized_lines_and_logs_following_diagnostics(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    server = StdioAppServer(
        [sys.executable, str(SERVER)], cwd=tmp_path, environ={}, agent_id=123, request_timeout=5.0
    )
    await server.start()
    try:
        assert await server.request("stderr_flood", {"bytes": 256 * 1024}) == {}
        assert await server.request("stderr_flood", {"bytes": 32 * 1024}) == {}
    finally:
        await server.close()
    rows = [record for record in loguru_records if record["message"].startswith("codex stderr: ")]
    assert [record["message"] for record in rows] == [
        "codex stderr: [stderr line exceeded the pipe reader limit]",
        "codex stderr: startup diagnostic",
        "codex stderr: " + "x" * 4096 + " [stderr line truncated]",
        "codex stderr: startup diagnostic",
    ]
    assert all(record["extra"]["agent_id"] == 123 for record in rows)
    assert all(record["level"].name == "INFO" for record in rows)


async def test_close_reaps_app_server_while_descendant_keeps_stderr_open(tmp_path: Path) -> None:
    server = StdioAppServer([sys.executable, str(SERVER)], cwd=tmp_path, environ={}, agent_id=123)
    await server.start()
    try:
        assert isinstance((await server.request("hold_stderr", {}))["pid"], int)
        await asyncio.wait_for(server.close(), timeout=2)
        assert server.returncode is not None
    finally:
        (tmp_path / "release-stderr").touch()
        await server.close()


async def test_codex_stderr_credentials_are_removed_from_otlp_payload(tmp_path: Path) -> None:
    captured: list[str] = []
    received = Event()

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/logs" and len(raw) > 16:
                batch = ExportLogsServiceRequest.FromString(raw)
                captured.extend(
                    record.body.string_value
                    for resource in batch.resource_logs
                    for scope in resource.scope_logs
                    for record in scope.log_records
                )
                received.set()
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    canary = "synthetic-stderr-fixture"
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as receiver:
        thread = Thread(target=receiver.serve_forever, daemon=True)
        thread.start()
        now = system_clock().now
        backend = OtlpBackend(
            endpoint=lambda: f"http://127.0.0.1:{receiver.server_port}",
            enabled=lambda: True,
            export_allowed=lambda: True,
            home=tmp_path,
            mirror_dir=tmp_path,
            instance_id="stderr-probe",
            now=now,
        )
        pipeline = telemetry.EventPipeline(writer=backend.export_batch, now=now)
        add_event_sink(
            home=tmp_path,
            process="stderr-probe",
            producer=lambda: pipeline,
            machine_reader=lambda: "probe-machine",
        )
        server = StdioAppServer(
            [sys.executable, str(SERVER)],
            cwd=tmp_path,
            environ={"OPENAI_API_KEY": canary},
            agent_id=123,
        )
        try:
            await server.start()
            assert await server.request("stderr_secret", {}) == {}
            assert await asyncio.to_thread(received.wait, 5)
            assert len(captured) == 1
            assert canary not in captured[0]
            assert "OPENAI_API_KEY=*** diagnostic_value=***" in captured[0]
            assert '"agent_id":123' in captured[0]
        finally:
            await server.close()
            release_event_sink()
            pipeline.stop()
            backend.shutdown()
            receiver.shutdown()
            thread.join()


async def test_each_rpc_uses_its_callers_current_trace(tmp_path: Path) -> None:
    server = StdioAppServer(
        [sys.executable, str(SERVER)], cwd=tmp_path, environ={}, trace_requests=True
    )
    await server.start()
    try:
        for parent in (1, 2):
            span = NonRecordingSpan(
                SpanContext(parent, parent, False, trace_flags=TraceFlags(TraceFlags.SAMPLED))
            )
            with use_span(span):
                assert await server.request("trace", {}) == {
                    "trace": {"traceparent": f"00-{parent:032x}-{parent:016x}-01"}
                }
        assert await server.request("trace", {}) == {"trace": None}
    finally:
        await server.close()


async def test_notification_reaches_the_handler(tmp_path: Path) -> None:
    seen: list[tuple[str, JsonObject]] = []
    server = transport(tmp_path)
    server.on_notification(lambda method, params: seen.append((method, params)))
    await server.start()
    try:
        await server.request("emit", {})
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == [("test/notification", {"value": 1})]
    finally:
        await server.close()


async def test_server_request_is_answered_on_the_same_pipe(tmp_path: Path) -> None:
    async def handler(method: str, params: JsonObject) -> JsonObject:
        assert method == "item/commandExecution/requestApproval"
        assert params == {"command": "echo hi"}
        return {"decision": "decline"}

    server = transport(tmp_path)
    server.on_request(handler)
    await server.start()
    try:
        result = await server.request("ask", {})
        assert result == {"answer": {"decision": "decline"}}
    finally:
        await server.close()


async def test_json_rpc_error_carries_the_code(tmp_path: Path) -> None:
    server = transport(tmp_path)
    await server.start()
    try:
        with pytest.raises(JsonRpcError) as info:
            await server.request("boom", {})
        assert info.value.code == -32000
        assert "boom" in info.value.message
    finally:
        await server.close()


async def test_request_times_out_and_fails_fast_after_the_peer_exits(tmp_path: Path) -> None:
    server = transport(tmp_path, timeout=0.2)
    await server.start()
    try:
        with pytest.raises(TimeoutError):
            await server.request("hang", {})
        await server.request("quit", {})
        for _ in range(100):
            if server.closed:
                break
            await asyncio.sleep(0.01)
        assert server.closed is True
        with pytest.raises(TransportClosedError):
            await server.request("echo", {})
    finally:
        await server.close()


async def test_close_terminates_the_process(tmp_path: Path) -> None:
    server = transport(tmp_path)
    assert server.returncode is None
    await server.start()
    pid = server.pid
    assert pid is not None
    await server.close()
    assert server.closed is True
    assert server.returncode is not None
    with pytest.raises(TransportClosedError):
        await server.request("echo", {})


@pytest.mark.parametrize("environment", [{}, {"SESSION_VALUE": "captured"}])
async def test_transport_copies_only_its_explicit_environment_before_start(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    supplied = dict(environment)
    server = StdioAppServer(
        [sys.executable, str(SERVER)], cwd=tmp_path, environ=supplied, request_timeout=5
    )
    supplied["SESSION_VALUE"] = "changed-after-construction"
    try:
        await server.start()
        observed = await server.request("environment", {})
        assert observed.get("SESSION_VALUE") == environment.get("SESSION_VALUE")
        assert "PYTEST_CURRENT_TEST" not in observed
        assert "MEIVA_HOME" not in observed
    finally:
        await server.close()


async def test_reply_above_eight_mib_is_received_whole(tmp_path: Path) -> None:
    payload_bytes = 10 * 1024 * 1024
    server = transport(tmp_path, timeout=15.0)
    await server.start()
    try:
        result = await server.request("large_reply", {"bytes": payload_bytes})
        thread = as_object(result.get("thread"))
        assert thread["id"] == "thread-big"
        blob = thread["blob"]
        assert isinstance(blob, str) and len(blob) == payload_bytes
        assert await server.request("echo", {}) == {"echo": "echo"}
    finally:
        await server.close()


async def test_trailing_frame_without_newline_at_eof_settles_request(tmp_path: Path) -> None:
    payload_bytes = 128 * 1024
    server = transport(tmp_path)
    await server.start()
    try:
        result = await server.request("eof_reply", {"bytes": payload_bytes})
        assert result["eof"] is True
        blob = result["blob"]
        assert isinstance(blob, str) and len(blob) == payload_bytes
    finally:
        await server.close()


@pytest.mark.parametrize("method", ["large_reply", "unterminated_flood"])
async def test_frame_exceeding_max_frame_bytes_closes_the_transport(
    tmp_path: Path, method: str
) -> None:
    server = transport(tmp_path, timeout=5.0, max_frame_bytes=4096)
    await server.start()
    try:
        with pytest.raises(TransportClosedError, match="stdout frame exceeded 4096 bytes"):
            _ = await server.request(method, {"bytes": 16384})
        assert server.closed is True
    finally:
        await server.close()
