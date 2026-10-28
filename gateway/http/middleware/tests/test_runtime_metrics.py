"""Gateway runtime and SSE lifecycle metric emission."""

from __future__ import annotations

import asyncio
import logging
import subprocess

import psutil
import pytest

from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.http.middleware import runtime_metrics
from gateway.http.middleware.loop_lag import LagPolicy

_POLICY = LagPolicy(tick_interval_s=0.01, emit_interval_s=0.03, slow_tick_threshold_ms=10_000.0)


async def _run_monitor_for(process: psutil.Process, seconds: float) -> None:
    monitor = runtime_metrics.GatewayRuntimeMonitor(
        loop=asyncio.get_running_loop(), process=process, policy=_POLICY
    )
    monitor.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        monitor.stop()


async def test_runtime_monitor_emits_process_and_loop_metrics_from_a_real_loop(
    emitted_events: EmittedEvents,
) -> None:
    await _run_monitor_for(psutil.Process(), 0.3)

    names = [event.event_name for event in emitted_events]
    assert names.count("gateway_event_loop") == names.count("gateway_process") >= 1
    loop_event = next(e for e in emitted_events if e.event_name == "gateway_event_loop")
    process_event = next(e for e in emitted_events if e.event_name == "gateway_process")
    assert loop_event.attributes["slow_ticks"] == 0
    assert 0.0 <= loop_event.attributes["lag_ms"] < 5_000.0
    assert process_event.attributes["rss_bytes"] > 0
    assert process_event.attributes["fd_count"] > 0
    assert process_event.attributes["cpu_percent"] >= 0.0


async def test_runtime_monitor_stops_sampling_once_stopped(
    emitted_events: EmittedEvents,
) -> None:
    await _run_monitor_for(psutil.Process(), 0.2)
    emitted_events.clear()
    await asyncio.sleep(0.15)
    assert list(emitted_events) == []


# meiva:note
# A process that is gone cannot be sampled; the monitor still reports the loop each window.
async def test_runtime_monitor_keeps_ticking_after_a_sampling_failure(
    emitted_events: EmittedEvents, caplog: pytest.LogCaptureFixture
) -> None:
    child = subprocess.Popen(["sleep", "60"])
    gone = psutil.Process(child.pid)
    child.kill()
    child.wait(timeout=10)

    with caplog.at_level(logging.WARNING):
        await _run_monitor_for(gone, 0.3)

    assert "gateway runtime metric sample failed" in caplog.text
    names = [event.event_name for event in emitted_events]
    assert names.count("gateway_event_loop") >= 2
    assert "gateway_process" not in names


# meiva:note
# The entry hands the monitor the process it samples; a pid read inside the module would report this
# test process instead of the child it was given. An idle `sleep` is orders of magnitude smaller than
# this interpreter, so its rss is the proof.
async def test_start_runtime_monitor_samples_the_process_the_entry_hands_it(
    emitted_events: EmittedEvents,
) -> None:
    child = subprocess.Popen(["sleep", "60"])
    child_process = psutil.Process(child.pid)
    try:
        before = child_process.memory_info().rss
        monitor = runtime_metrics.start_runtime_monitor(process=child_process, policy=_POLICY)
        try:
            await asyncio.sleep(0.3)
            after = child_process.memory_info().rss
        finally:
            monitor.stop()
    finally:
        child.kill()
        child.wait(timeout=10)

    process_event = next(e for e in emitted_events if e.event_name == "gateway_process")
    rss = process_event.attributes["rss_bytes"]
    assert min(before, after) <= rss <= max(before, after)
    assert rss < psutil.Process().memory_info().rss


def test_sse_metrics_initialize_idle_modes_at_zero(
    emitted_events: EmittedEvents,
) -> None:
    runtime_metrics.sse_opened("filtered")
    emitted_events.clear()

    runtime_metrics.initialize_sse_metrics()

    assert [event.attributes for event in emitted_events] == [
        {"mode": "filtered", "active_connections": 0},
        {"mode": "throttled", "active_connections": 0},
        {"mode": "merged", "active_connections": 0},
    ]
    assert runtime_metrics.sse_active_connections == {
        "filtered": 0,
        "throttled": 0,
        "merged": 0,
    }
