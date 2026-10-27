# meiva:note
# In-memory stand-ins for the OTel SDK's logger and meter providers, with hooks at each call the
# OTLP backend makes, so a test can block, fail or observe one step of the backend's lifecycle.

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class FakeInstrument:
    def __init__(self, on_metric: Callable[[int | float], None] | None = None) -> None:
        self._on_metric = on_metric

    def add(self, value: int | float, _attributes: Any = None) -> None:
        if self._on_metric is not None:
            self._on_metric(value)

    record = add


class FakeMeter:
    def __init__(self, on_metric: Callable[[int | float], None] | None = None) -> None:
        self._on_metric = on_metric

    def create_counter(
        self, _name: str, *, unit: str = "", description: str = ""
    ) -> FakeInstrument:
        del unit, description
        return FakeInstrument(self._on_metric)

    create_histogram = create_counter

    def create_observable_gauge(
        self, _name: str, *, callbacks: Any = None, unit: str = "", description: str = ""
    ) -> object:
        del callbacks, unit, description
        return object()


class FakeLogger:
    def __init__(self, on_emit: Callable[[Any], None] | None = None) -> None:
        self._on_emit = on_emit

    def emit(self, record: Any) -> None:
        if self._on_emit is not None:
            self._on_emit(record)


class FakeProvider:
    def __init__(
        self,
        *,
        on_emit: Callable[[Any], None] | None = None,
        on_get_meter: Callable[[], object] | None = None,
        on_metric: Callable[[int | float], None] | None = None,
        on_flush: Callable[[int], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
    ) -> None:
        self.closed = threading.Event()
        self.flushed = threading.Event()
        self._on_emit = on_emit
        self._on_get_meter = on_get_meter
        self._on_metric = on_metric
        self._on_flush = on_flush
        self._on_shutdown = on_shutdown

    def get_logger(self, _name: str) -> FakeLogger:
        return FakeLogger(self._on_emit)

    def get_meter(self, _name: str) -> object:
        if self._on_get_meter is not None:
            return self._on_get_meter()
        return FakeMeter(self._on_metric)

    def force_flush(self, *, timeout_millis: int) -> None:
        if self._on_flush is not None:
            self._on_flush(timeout_millis)
        self.flushed.set()

    def shutdown(self) -> None:
        if self._on_shutdown is not None:
            self._on_shutdown()
        self.closed.set()


class BlockingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.teardown_entered = threading.Event()
        self.teardown_entered_at = 0.0
        self.release = threading.Event()

    def shutdown(self) -> None:
        self.teardown_entered_at = time.monotonic()
        self.teardown_entered.set()
        assert self.release.wait(5)
        self.closed.set()
