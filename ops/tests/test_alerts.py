# meiva:note
# The alert publish shell against the throwaway Redis: a real subscriber receives the frame, and a
# refused port stands for an unreachable Redis. Nothing is patched.

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus, EventBusConfig
from base.host.net.tests.local_http import closed_port_url
from ops.alerts import ALERTS_CHANNEL, publish_alert_rows

_STARTS = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _row(alert_id: int = 1) -> dict[str, object]:
    return {
        "id": alert_id,
        "status": "unresolved",
        "severity": "critical",
        "alertname": "DiskFull",
        "labels": {"host": "m1"},
        "annotations": {},
        "starts_at": _STARTS,
        "ends_at": None,
        "fingerprint": f"fp-{alert_id}",
        "generator_url": "http://grafana.invalid/alerting",
        "source": "grafana",
        "notified_at": None,
        "created_at": _STARTS,
        "updated_at": _STARTS,
    }


def _unreachable_bus() -> EventBus:
    url = closed_port_url().replace("http://", "redis://") + "/0"
    return EventBus(EventBusConfig(redis_url=url, events_channel="meiva:events"), rng=seeded_rng())


# meiva:note
# Each upserted row reaches a real subscriber of the alerts channel as its validated JSON frame.
def test_each_row_is_published_to_the_alerts_channel(event_bus: EventBus) -> None:
    client = event_bus.sync_redis(decode_responses=True)
    subscription = cast("Any", client).pubsub()
    try:
        subscription.subscribe(ALERTS_CHANNEL)
        confirmation = subscription.get_message(timeout=2.0)
        assert confirmation is not None and confirmation["type"] == "subscribe"

        publish_alert_rows(event_bus, [_row(1), _row(2)])

        frames: list[dict[str, Any]] = []
        while (message := subscription.get_message(timeout=1.0)) is not None:
            if message["type"] == "message":
                frames.append(json.loads(str(message["data"])))
    finally:
        subscription.close()
        client.close()

    assert [frame["id"] for frame in frames] == [1, 2]
    assert frames[0]["alertname"] == "DiskFull" and frames[0]["labels"] == {"host": "m1"}


# meiva:note
# A Redis outage must not fail the ingest: the publish is a live tail, so the failure is logged
# as a warning and swallowed. The test requests the Redis resource only to open the suite's
# Redis window; the bus it publishes through points at a refused port.
@pytest.mark.usefixtures("redis_url")
def test_an_unreachable_redis_is_a_warning_not_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ops.alerts"):
        publish_alert_rows(_unreachable_bus(), [_row()])

    assert any("SSE publish failed" in record.getMessage() for record in caplog.records)


# meiva:note
# A batch with no rows touches no connection, even a dead one.
def test_no_rows_means_no_connection(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ops.alerts"):
        publish_alert_rows(_unreachable_bus(), [])

    assert caplog.records == []


# meiva:note
# A row that is not a valid alert is a bug, not an outage: it raises before anything is
# published instead of hiding behind the "Redis unreachable" warning.
def test_a_malformed_row_raises_instead_of_being_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ValidationError):
        publish_alert_rows(_unreachable_bus(), [_row(), {"id": 2}])

    assert caplog.records == []
