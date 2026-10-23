from __future__ import annotations

import asyncio
from typing import Literal, cast

import pytest
from fastapi import Request

from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.events import sse
from gateway.events.sse_frames import SseCadence
from gateway.http.middleware import runtime_metrics


class _Request:
    async def is_disconnected(self) -> bool:
        return True


@pytest.mark.parametrize(
    "mode",
    ("filtered", "throttled"),
)
def test_sse_stream_lifecycle_increments_and_decrements_active_gauge(
    event_bus: EventBus,
    mode: Literal["filtered", "throttled"],
    emitted_events: EmittedEvents,
) -> None:

    runtime_metrics.initialize_sse_metrics()
    emitted_events.clear()

    async def run_stream() -> None:
        request = cast(Request, _Request())
        if mode == "filtered":
            stream = sse.event_stream(event_bus, 7, request, poll_s=0.25, cadence=SseCadence())
        else:
            stream = sse.throttled_event_stream(
                event_bus, request, throttle_rate=10.0, cadence=SseCadence()
            )
        assert await anext(stream) == b": stream open\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(run_stream())

    assert [event.attributes for event in emitted_events] == [
        {"mode": mode, "active_connections": 1, "opened": 1},
        {"mode": mode, "active_connections": 0, "closed": 1},
    ]
