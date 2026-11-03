"""FIFO receipts belonging to the OTLP backend's sole writer."""

from __future__ import annotations

import threading

from base.telemetry.delivery.receipts import Event


class WorkerFlushMarker:
    """A distinct receipt for all records preceding this marker in the queue."""

    def __init__(self) -> None:
        self.done = threading.Event()


# meiva:note
# One slot of the OTLP worker's FIFO queue: an event, a flush marker, or the stop sentinel.
type QueueItem = Event | WorkerFlushMarker | None
