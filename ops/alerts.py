# meiva:note
# The alert row and its live SSE channel (best-effort publish).
#
# Shared by the gateway's alert endpoints and the processes that raise or repair alerts without
# serving HTTP (the events-maintenance service's Grafana reconciliation), so both publish the same
# validated row shape through the same code.

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from base.events.live.bus import EventBus

_log = logging.getLogger(__name__)

# meiva:note
# Store vocabulary (user design 2026-08-12): three severities, two statuses.
AlertSeverity = Literal["critical", "warning", "error"]
AlertStatus = Literal["unresolved", "resolved"]


# meiva:note
# One alerts row as the UI sees it (list + SSE frames).
class AlertRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    status: AlertStatus
    severity: AlertSeverity
    alertname: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime
    ends_at: datetime | None
    fingerprint: str
    generator_url: str
    source: str
    notified_at: datetime | None
    created_at: datetime
    updated_at: datetime


# meiva:note
# The Redis pub/sub channel every ingest publishes to and the SSE stream subscribes to.
ALERTS_CHANNEL = "meiva:alerts"


# meiva:note
# Publish each upserted row to the SSE channel (best-effort).
#
# A Redis outage must not fail the ingest: the SSE stream is a live tail and the UI's initial
# fetch carries the same rows. A row that is not an `AlertRow` is still a bug and raises before
# anything is published, because it is validated outside the best-effort guard.
#
# The `pyright: ignore` on the publish call covers redis-py's `from_url` kwargs, which it types as
# Unknown (the same pattern as base/events/live/redis_client.py).
def publish_alert_rows(bus: EventBus, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        return
    frames = [AlertRow.model_validate(row).model_dump_json() for row in rows]
    try:
        with bus.sync_redis() as client:
            for frame in frames:
                client.publish(ALERTS_CHANNEL, frame)  # pyright: ignore[reportUnknownMemberType]
    except Exception:
        _log.warning("alerts: SSE publish failed (Redis unreachable?)", exc_info=True)
