from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base import telemetry
from base.cluster.machine import MachineIdentityReader
from base.host.env import port_lease
from base.log.tests.log_records import LogRecords, loguru_records
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry.delivery.pipeline import EventPipeline
from services.entrypoints.gate import daemon
from services.entrypoints.gate.daemon import Gate, GateConfig, GateServer

__all__ = ["loguru_records"]

_STATIC = Path(daemon.__file__).parent / "static"
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _idle_server(server_class: type[GateServer] = GateServer) -> GateServer:
    gate = Gate(
        gateway_base="http://127.0.0.1:1",
        app_base="http://127.0.0.1:2",
        static_dir=_STATIC,
        config=GateConfig(home=Path("/unused-gate-home")),
    )
    return server_class(("127.0.0.1", 0), gate)


class _InterruptedServer(GateServer):
    def serve_forever(self, poll_interval: float = 0.5) -> None:
        raise KeyboardInterrupt


class _FailingServer(GateServer):
    serve_failure: BaseException | None = None
    close_failure: BaseException | None = None

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if self.serve_failure is not None:
            raise self.serve_failure

    def server_close(self) -> None:
        super().server_close()
        if self.close_failure is not None:
            raise self.close_failure


def _failing(
    serve_failure: BaseException | None, close_failure: BaseException | None
) -> _FailingServer:
    server = _idle_server(_FailingServer)
    assert isinstance(server, _FailingServer)
    server.serve_failure = serve_failure
    server.close_failure = close_failure
    return server


@pytest.mark.parametrize(
    ("url", "expected"),
    [("http://localhost:4100/", 4100), ("http://localhost/", 3000), ("", 3000)],
)
def test_the_entry_port_is_the_url_port_or_3000(url: str, expected: int) -> None:
    assert daemon.entry_port_of(url) == expected


def test_an_interrupt_ends_serving_and_closes_the_listener() -> None:
    server = _idle_server(_InterruptedServer)
    daemon.serve(server)
    assert server.socket.fileno() == -1


def test_a_serve_failure_keeps_its_error_and_notes_a_close_failure() -> None:
    primary = SystemExit("serve failed")
    server = _failing(primary, OSError("close failed"))
    with pytest.raises(SystemExit) as caught:
        daemon.serve(server)
    assert caught.value is primary
    assert any("close failed" in note for note in primary.__notes__)
    assert server.socket.fileno() == -1


def test_a_close_failure_without_a_serve_failure_is_raised() -> None:
    failure = OSError("close failed")
    with pytest.raises(OSError) as caught:
        daemon.serve(_failing(None, failure))
    assert caught.value is failure


def _event() -> telemetry.Event:
    return telemetry.Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="test",
        category="log",
        event_name="log",
        level="info",
        source="test",
        target_agent_id=None,
        attributes={},
    )


def _pipeline_whose_writer_fails(failure: BaseException) -> EventPipeline:
    failed = threading.Event()

    def writer(_events: list[telemetry.Event]) -> None:
        failed.set()
        raise failure

    pipeline = EventPipeline(writer=writer, flush_interval_s=0.01)
    pipeline.enqueue(_event())
    assert failed.wait(2.0)
    return pipeline


def test_a_writer_failure_at_stop_is_raised_without_an_error_in_flight() -> None:
    failure = RuntimeError("writer failed")
    pipeline = _pipeline_whose_writer_fails(failure)
    with pytest.raises(RuntimeError) as caught:
        daemon.stop_pipeline(pipeline, None)
    assert caught.value is failure


def test_a_writer_failure_at_stop_is_a_note_on_the_error_in_flight() -> None:
    primary = SystemExit("serve failed")
    pipeline = _pipeline_whose_writer_fails(RuntimeError("writer failed"))
    daemon.stop_pipeline(pipeline, primary)
    assert any("writer failed" in note for note in primary.__notes__)


def test_an_unfinished_stop_is_reported_once_and_not_retried(loguru_records: LogRecords) -> None:
    entered = threading.Event()
    release = threading.Event()

    def writer(_events: list[telemetry.Event]) -> None:
        entered.set()
        release.wait(5.0)

    pipeline = EventPipeline(writer=writer, flush_interval_s=0.01)
    pipeline.enqueue(_event())
    assert entered.wait(2.0)
    try:
        daemon.stop_pipeline(pipeline, None, timeout_s=0.05)
        reports = [m for m in loguru_records.messages() if "event pipeline stop unfinished" in m]
        assert len(reports) == 1
        assert not pipeline.stopped
    finally:
        release.set()
        pipeline.stop(timeout=2)


def test_a_clean_writer_stop_reports_nothing(loguru_records: LogRecords) -> None:
    pipeline = EventPipeline(writer=lambda _events: None, flush_interval_s=0.01)
    daemon.stop_pipeline(pipeline, None)
    assert pipeline.stopped
    assert not [m for m in loguru_records.messages() if "unfinished" in m]


def test_the_database_factory_shares_one_gate_between_opens() -> None:
    from base.cluster.machine.configured import ConfiguredMachineIdentity
    from base.config import ConfigBoot

    config = ConfigBoot()
    machine: MachineIdentityReader = ConfiguredMachineIdentity(config)
    factory = daemon.gate_database(
        image=LoadedCommit.capture(), machine=machine, config=config, home=meiva_home()
    )
    first, second = factory(), factory()
    assert first is not second
    assert first._gate is second._gate
    assert first._gate.application_name().startswith("meiva:gate:v")


def _child_env(home: Path) -> dict[str, str]:
    (home / ".env").write_text(
        "MEIVA_MACHINE_NAME=gate-test\nMEIVA_TIMEZONE=UTC\nMEIVA_TELEMETRY_OTLP_ENABLED=false\n",
        encoding="utf-8",
    )
    return {**os.environ, "MEIVA_HOME": str(home)}


def _start_entry(home: Path, *args: str) -> subprocess.Popen[str]:
    # meiva:note
    # the interpreter running the tests and the module under test, no shell.
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "services.entrypoints.gate.daemon", *args],
        cwd=_REPO_ROOT,
        env=_child_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def _health(port: int) -> dict[str, object] | None:
    try:
        # meiva:note
        # a loopback URL built from the child's own port.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/__meiva/healthz", timeout=1) as r:
            return json.loads(r.read())
    except OSError:
        return None


def test_invalid_arguments_start_nothing(tmp_path: Path) -> None:
    child = _start_entry(tmp_path, "--unknown-option")
    output, _ = child.communicate(timeout=60)
    assert child.returncode == 2
    assert "unrecognized arguments" in output
    assert not (tmp_path / "logs").exists()


def test_the_entry_serves_until_stopped_and_then_exits_cleanly(tmp_path: Path) -> None:
    port = _free_port()
    child = _start_entry(tmp_path, "--port", str(port))
    try:
        deadline = time.monotonic() + 60
        served = None
        while served is None and time.monotonic() < deadline:
            assert child.poll() is None, "the entry exited before serving"
            served = _health(port)
            time.sleep(0.05)
        assert served is not None, "the entry never served"
        assert served["name"] == "gate"
        assert served["pid"] == child.pid
        child.send_signal(signal.SIGTERM)
        output, _ = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode == 0, output
    assert _health(port) is None
    log = (tmp_path / "logs" / "gate.log").read_text(encoding="utf-8")
    assert f"gate serving on :{port}" in log
    assert "received SIGTERM" in log
    assert list((tmp_path / "logs").glob("events-*.jsonl"))


def test_a_taken_port_fails_the_entry_with_the_bind_error(tmp_path: Path) -> None:
    with socket.socket() as taken:
        # meiva:note
        # the entry binds every interface, so the port is only taken when its holder does too.
        taken.bind(("0.0.0.0", 0))  # noqa: S104
        taken.listen()
        child = _start_entry(tmp_path, "--port", str(taken.getsockname()[1]))
        output, _ = child.communicate(timeout=60)
    assert child.returncode != 0
    assert "Address already in use" in output
