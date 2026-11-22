# meiva:note
# State-gauge emissions for root diagnostics and trace disk-watermark checks.

from __future__ import annotations

import asyncio
from pathlib import Path

from base.agents.context.clients import ClientSet
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.daemon.health import DaemonProbe
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.supervision.meiva_root_glue.diagnostics import (
    Diagnostic,
    DiagnosticMonitor,
    RootHealthRounds,
)
from services.supervision.meiva_root_glue.tests.slices import system_clock


class _NoOpTelemetry:
    def start(
        self,
        *,
        name: str,
        producer: object,
        machine_reader: object,
        image: LoadedCommit,
        home: Path,
    ) -> None:
        del name, producer, machine_reader, image, home

    def sync(self, *, timeout: float) -> object:
        del timeout
        return None


class _ReadyHealth:
    async def run_round(self) -> None:
        return None

    def health_snapshot(self) -> dict[str, object]:
        return {}

    async def stop(self) -> None:
        return None


async def test_diagnostic_state_gauge_reflects_latest_definitive_sample_and_recovers(
    emitted_events: EmittedEvents,
) -> None:
    verdict = DaemonProbe.down("129 reminder notice(s) delivered in 24h without telemetry")
    monitor = DiagnosticMonitor(
        [Diagnostic("diagnostic-canary", lambda: verdict, interval_s=0.001)],
        clock=system_clock(),
    )
    await monitor.run_round()
    states = [e.attributes for e in emitted_events if e.event_name == "root_diagnostic_state"]
    assert states == [{"diagnostic": "diagnostic-canary", "ok": 0}]

    verdict = DaemonProbe.unavailable("db busy")
    await asyncio.sleep(0.002)
    await monitor.run_round()
    states = [e.attributes for e in emitted_events if e.event_name == "root_diagnostic_state"]
    assert states[-1] == {"diagnostic": "diagnostic-canary", "ok": 0}

    verdict = DaemonProbe.up("129 reminder notice(s) delivered and 129 stored")
    await asyncio.sleep(0.002)
    await monitor.run_round()
    states = [e.attributes for e in emitted_events if e.event_name == "root_diagnostic_state"]
    assert states[-1] == {"diagnostic": "diagnostic-canary", "ok": 1}


async def test_root_health_round_emits_trace_recording_disabled_gauge(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    usage = (0.95, 1024)
    rounds = RootHealthRounds(
        _ReadyHealth(),
        DiagnosticMonitor([], clock=system_clock()),
        clients=ClientSet(),
        image=LoadedCommit(source_root=Path(__file__).resolve().parents[4], sha=None),
        machine=machine_identity,
        telemetry=_NoOpTelemetry(),
        clock=system_clock(),
        home=tmp_path,
        trace_enabled=True,
        trace_disk_watermark=0.9,
        trace_disk_usage=lambda _path: usage,
    )
    await rounds.run_round()
    trace_states = [e.attributes for e in emitted_events if e.event_name == "trace_recording"]
    assert trace_states == [{"disabled": 1}]

    usage = (0.50, 1024 * 1024)
    await rounds.run_round()
    trace_states = [e.attributes for e in emitted_events if e.event_name == "trace_recording"]
    assert trace_states == [{"disabled": 1}, {"disabled": 0}]
