from __future__ import annotations

import math
import threading
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass

TRACE_INIT_WAIT_S = 30.0


@dataclass(frozen=True)
class RecordingReading:
    ready: bool
    first_timeout: bool


# meiva:note
# A missing Future means no attempt was admitted. Reservation creates a pending Future; only
# entry to the real initializer makes it running. Closing can cancel pending work, never work
# already running. A timeout spends only the observation budget, allowing genuine late success.
class TraceReadiness:
    def __init__(self) -> None:
        self._result: Future[bool] | None = None
        self._lock = threading.Lock()
        self._wait_expired = False

    def reserve(self) -> bool:
        with self._lock:
            if self._result is not None:
                return False
            self._result = Future()
            return True

    def begin(self) -> bool:
        # meiva:note
        # Enter the unique admitted initializer, unless close cancelled it first.
        with self._lock:
            if self._result is None:
                raise RuntimeError("trace initialization has no outstanding admission")
            if self._result.cancelled():
                return False
            return self._result.set_running_or_notify_cancel()

    def complete(self, *, recording: bool) -> None:
        with self._lock:
            if self._result is None or not self._result.running():
                raise RuntimeError("trace initialization has no outstanding admission")
            self._result.set_result(recording)

    def close(self) -> None:
        with self._lock:
            if self._result is None:
                self._result = Future()
            self._result.cancel()

    @property
    def resolved(self) -> bool:
        with self._lock:
            return self._result is not None and self._result.done()

    @property
    def ready(self) -> bool:
        with self._lock:
            result = self._result
        if result is None or not result.done() or result.cancelled():
            return False
        return result.result()

    def read(self, *, timeout: float) -> RecordingReading:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("trace initialization wait must be finite and non-negative")
        with self._lock:
            result = self._result
            wait = result is not None and not result.done() and not self._wait_expired
        if result is None or not wait:
            return RecordingReading(ready=self.ready, first_timeout=False)
        try:
            return RecordingReading(ready=result.result(timeout=timeout), first_timeout=False)
        except CancelledError:
            return RecordingReading(ready=False, first_timeout=False)
        except TimeoutError:
            with self._lock:
                first_timeout = not self._wait_expired
                self._wait_expired = True
            return RecordingReading(ready=self.ready, first_timeout=first_timeout)


# meiva:note
# Process tracing and billing observe the same admitted SDK attempt. The process root passes this
# resource to initialization; importing it reserves no attempt and starts no worker.
PROCESS_RECORDING = TraceReadiness()
