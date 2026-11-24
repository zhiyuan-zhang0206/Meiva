"""CLI dispatch drains its first telemetry batch before interpreter shutdown."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

_CHILD = Path(__file__).with_name("bootstrap") / "child_telemetry_exit.py"


@pytest.fixture
def receiver() -> Generator[tuple[http.server.ThreadingHTTPServer, list[bytes], list[float]]]:
    bodies: list[bytes] = []
    arrivals: list[float] = []

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/logs" and not body.startswith(b"{"):
                bodies.append(body)
                arrivals.append(time.time())
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, bodies, arrivals
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode", ["success", "exception", "empty", "unavailable"])
def test_cli_first_event_reaches_otlp_before_exit(
    tmp_path: Path,
    mode: str,
    receiver: tuple[http.server.ThreadingHTTPServer, list[bytes], list[float]],
) -> None:
    server, bodies, arrivals = receiver
    if mode == "unavailable":
        server.shutdown()
        server.server_close()
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(
        MEIVA_HOME=str(tmp_path / "home"),
        MEIVA_CONFIG_FETCH="skip",
        MEIVA_TELEMETRY_OTLP_ENABLED="true",
        MEIVA_TELEMETRY_OTLP_ENDPOINT=f"http://127.0.0.1:{server.server_address[1]}",
    )
    child = subprocess.run(  # noqa: S603 — fixed program and this checkout's interpreter
        [sys.executable, "-I", str(_CHILD), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    # The child holds the command open for several default flush intervals with a 60 s pipeline
    # timer: any request that arrived meanwhile came from a timer, not from the drain after it.
    (line,) = [
        line for line in child.stdout.splitlines() if line.startswith("command_returned_at=")
    ]
    returned_at = float(line.removeprefix("command_returned_at="))
    assert all(arrival >= returned_at for arrival in arrivals), (arrivals, returned_at)
    names = _log_names(bodies)
    if mode in ("empty", "unavailable"):
        assert not bodies
    else:
        assert "health_probe_ran" in names, child.stderr


def _log_names(bodies: list[bytes]) -> list[str]:
    names: list[str] = []
    for body in bodies:
        request = logs_service_pb2.ExportLogsServiceRequest.FromString(body)
        for resource in request.resource_logs:
            for scope in resource.scope_logs:
                for record in scope.log_records:
                    names.append(json.loads(record.body.string_value)["event_name"])
    return names
