"""Exit-seam delivery — a short-lived process's tail record reaches the OTLP
receiver (task #4320).

Under the SDK default (`shutdown_on_exit=True`) each provider registers its own
atexit shutdown at bring-up — mid-life, so later than
`base.telemetry.drain_on_exit` (registered at import) — and atexit runs
LIFO: the provider shut down FIRST, and a record emitted inside the drain
thread's final batch window was flushed by the exit drain into a shut-down
processor (`force_flush` returns False) and stayed mirror-only (task #4314
triage: exec-child `result/write` = 0 fleet-wide in Loki while the mirror held
every line; hierarchy-job tail lines 166 → 0 mirror→Loki). The providers are
now built with `shutdown_on_exit=False` and `_drain_on_exit` is the single
ordered exit seam.

This test runs a real short-lived child against a minimal OTLP/HTTP receiver
recording every POST body, and reads both channels: the receiver's exported
batches and the child's JSONL mirror.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

_POSTS: list[tuple[str, bytes]] = []
_POSTS_LOCK = threading.Lock()


class _ReceiverHandler(http.server.BaseHTTPRequestHandler):
    """Records every POST body and answers 200 — the reachability probe and
    the OTLP exports both pass through this one handler."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        with _POSTS_LOCK:
            _POSTS.append((self.path, body))
        self.acknowledge(body)

    def acknowledge(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def _serve_receiver(handler: type[_ReceiverHandler]) -> Generator[http.server.ThreadingHTTPServer]:
    _POSTS.clear()
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def otlp_receiver() -> Iterator[http.server.ThreadingHTTPServer]:
    with _serve_receiver(_ReceiverHandler) as server:
        yield server


_CHILD_SCRIPT = Path(__file__).with_name("_otlp_exit_delivery_child.py")


# The external CLI's `exec` command runs the caller's code inside this same
# attachment context. The lease and checkpoint dependencies are local fakes;
# the telemetry emitter, SDK wrapper, OTLP setup, and normal interpreter exit
# are real. `meiva.files.read` is one tail sdk_call (under 0.5 s before exit).


def _received_log_records(posts: list[tuple[str, bytes]]) -> list[Any]:
    """Every OTLP LogRecord in the receiver's `/v1/logs` POST bodies."""
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    records: list[Any] = []
    for path, raw in posts:
        if not path.endswith("/v1/logs") or len(raw) < 4:
            continue  # the reachability probe posts a small JSON body
        request = logs_service_pb2.ExportLogsServiceRequest()
        with contextlib.suppress(Exception):
            request.ParseFromString(raw)
        for resource_logs in request.resource_logs:
            for scope_logs in resource_logs.scope_logs:
                records.extend(scope_logs.log_records)
    return records


def _marker_seq_of(record: Any) -> tuple[object, object]:
    attrs: dict[str, Any] = {}
    for kv in record.attributes:
        if kv.value.HasField("string_value"):
            attrs[kv.key] = kv.value.string_value
    payload: dict[str, Any] = {}
    if record.body.string_value:
        with contextlib.suppress(Exception):
            payload = json.loads(record.body.string_value).get("attributes") or {}
    return payload.get("marker", attrs.get("marker")), payload.get("seq")


def _sent_pairs() -> set[tuple[object, object]]:
    """Decode the receiver's OTLP/HTTP log batches into (marker, seq) pairs.

    The record carries both as OTLP attributes and the event body is the
    mirror-shape JSON, so either channel identifies the record."""
    with _POSTS_LOCK:
        posts = list(_POSTS)
    return {_marker_seq_of(record) for record in _received_log_records(posts)}


def _run_child(
    home: Path, marker: str, server: http.server.ThreadingHTTPServer, *, multi_batch: bool = False
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "MEIVA_HOME": str(home),
            "MEIVA_PROCESS_PROFILE": "agent",
            # The explicit endpoint + enabled flag open the export gate
            # (tests/fixtures/env_bootstrap.py disables OTLP suite-wide).
            "MEIVA_TELEMETRY_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            "MEIVA_TELEMETRY_OTLP_ENABLED": "true",
        }
    )
    env.pop("MEIVA_LOG_DIR", None)
    # meiva:note
    # Fixed argv, sys.executable is trusted. -I keeps the parent's PYTHON*
    # environment out; `import base` resolves through the checkout's
    # editable install.
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(_CHILD_SCRIPT),
            marker,
            *(["multi"] if multi_batch else []),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def test_tail_record_of_a_short_lived_process_reaches_the_receiver(
    tmp_path: Path, otlp_receiver: http.server.ThreadingHTTPServer
) -> None:
    marker = uuid.uuid4().hex
    home = tmp_path / "home"
    proc = _run_child(home, marker, otlp_receiver)
    assert proc.returncode == 0, proc.stderr

    deadline = time.monotonic() + 10.0
    pairs: set[tuple[object, object]] = set()
    while time.monotonic() < deadline:
        pairs = _sent_pairs()
        if (marker, "tail") in pairs:
            break
        time.sleep(0.05)

    # The pre-tail record proves the receiver decoded real traffic.
    assert (marker, "early") in pairs, f"receiver saw: {sorted(pairs)!r}"
    # Red before the fix (task #4320): the exit flush landed on a shut-down
    # processor and only the JSONL mirror kept this record.
    assert (marker, "tail") in pairs, f"receiver saw: {sorted(pairs)!r}"

    # The mirror carries the same record — the local channel never lost it.
    rows = [
        json.loads(line)
        for path in sorted((home / "logs").glob("events-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    mirror_tail: list[Any] = []
    for row in rows:
        attributes = cast("dict[str, Any]", row.get("attributes") or {})
        if attributes.get("marker") == marker and attributes.get("seq") == "tail":
            mirror_tail.append(row)
    assert mirror_tail, "the JSONL mirror must hold the tail record"


@pytest.mark.parametrize("header_delay", [0.0, 2.8])
def test_stalled_log_acknowledgement_is_accounted_before_exit(
    tmp_path: Path, header_delay: float
) -> None:
    stalled = threading.Event()
    release = threading.Event()

    class StalledReceiver(_ReceiverHandler):
        def acknowledge(self, body: bytes) -> None:
            if self.path == "/v1/logs" and b'"seq":"tail"' in body:
                _ = release.wait(header_delay)
                acknowledgement = ExportLogsServiceResponse()
                acknowledgement.partial_success.error_message = "fixture acknowledgement"
                payload = acknowledgement.SerializeToString()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                stalled.set()
                if not release.wait(2.8 if header_delay else 10):
                    with contextlib.suppress(OSError):
                        _ = self.wfile.write(payload)
                self.close_connection = True
            else:
                super().acknowledge(body)

    try:
        with _serve_receiver(StalledReceiver) as receiver:
            marker = uuid.uuid4().hex
            result = _run_child(tmp_path / "home", marker, receiver)
            assert stalled.is_set()
            assert (marker, "early") in _sent_pairs()
            assert result.returncode == 0, result.stderr
            with _POSTS_LOCK:
                pending = [(path, raw) for path, raw in _POSTS if b'"seq":"tail"' in raw]
            assert len(pending) == 1
            count = len(_received_log_records(pending))
            assert count > 0
            assert (
                f"OTLP log export failed for {count} record(s), error.type=Timeout" in result.stderr
            )
            assert "unfinished" not in result.stderr
            assert _metric_export_failures() == count
    finally:
        release.set()


def _metric_export_failures() -> int:
    totals: dict[str, int] = {}
    with _POSTS_LOCK:
        posts = list(_POSTS)
    for path, raw in posts:
        if path != "/v1/metrics":
            continue
        metrics = ExportMetricsServiceRequest.FromString(raw)
        points = (
            point
            for resource in metrics.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == "otel.sdk.exporter.log.exported"
            for point in metric.sum.data_points
        )
        for point in points:
            attributes = {attr.key: attr.value.string_value for attr in point.attributes}
            if cause := attributes.get("error.type"):
                totals[cause] = max(totals.get(cause, 0), point.as_int)
    return sum(totals.values())


def test_shutdown_accounts_for_both_inflight_and_pending_batches(tmp_path: Path) -> None:
    release = threading.Event()
    home = tmp_path / "home"

    class SlowReceiver(_ReceiverHandler):
        def acknowledge(self, body: bytes) -> None:
            if self.path == "/v1/logs" and (b'"seq":"bulk"' in body or b'"seq":"tail"' in body):
                if b'"seq":"bulk"' in body:
                    (home / "first-batch").touch()
                _ = release.wait(7)
                self.close_connection = True
            else:
                super().acknowledge(body)

    try:
        with _serve_receiver(SlowReceiver) as receiver:
            result = _run_child(home, uuid.uuid4().hex, receiver, multi_batch=True)
            assert result.returncode == 0, result.stderr
            assert (home / "first-batch").exists()
            _assert_shutdown_batch_counts(result)
    finally:
        release.set()


def _assert_shutdown_batch_counts(result: subprocess.CompletedProcess[str]) -> None:
    with _POSTS_LOCK:
        pending = [
            (path, raw) for path, raw in _POSTS if b'"seq":"bulk"' in raw or b'"seq":"tail"' in raw
        ]
    assert len(pending) == 2
    batches = [_received_log_records([post]) for post in pending]
    tagged = [_marker_seq_of(record)[1] for batch in batches for record in batch]
    assert tagged.count("bulk") == 512
    assert tagged.count("tail") == 3
    counts = sorted(map(len, batches))
    losses = sorted(
        int(count)
        for count in re.findall(r"OTLP log export failed for (\d+) record", result.stderr)
    )
    assert losses == counts, result.stderr
    assert "unfinished" not in result.stderr
    assert _metric_export_failures() == sum(counts)
