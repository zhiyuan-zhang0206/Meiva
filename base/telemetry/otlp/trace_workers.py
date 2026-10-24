"""Owned trace initialization and collector retry workers for one process."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable

from base.telemetry.delivery.reporting import report_no_pipeline


class TraceArm:
    """One arm attempt; late success remains allowed until its real stop request."""

    def __init__(self, endpoint: str, initialize: Callable[[str, threading.Event], None]) -> None:
        self._endpoint = endpoint
        self._initialize = initialize
        self._stop_requested = threading.Event()
        self.finished = threading.Event()
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        # meiva:note
        # Launch once, or decline if stop closed admission before this call.
        with self._lock:
            if self._stop_requested.is_set():
                return False
            if self._thread is not None:
                raise RuntimeError("trace arm was already started")
            self._thread = threading.Thread(target=self._run, daemon=True, name="trace-arm")
            thread = self._thread
        thread.start()
        return True

    def _run(self) -> None:
        try:
            self._initialize(self._endpoint, self._stop_requested)
        except BaseException as exc:
            self._error = exc
            report_no_pipeline("[trace-arm] worker failed: {err}", err=repr(exc), exc=exc)
        finally:
            self.finished.set()

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested.set()
            if self._thread is None:
                self.finished.set()

    def stop(self, timeout: float = 2.0) -> bool:
        """Observe the same arm finitely; this cannot interrupt a blocked SDK call."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("trace stop timeout must be finite and non-negative")
        self.request_stop()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        # meiva:note
        # A start call may still be in flight, or may have raised before confirming native
        # startup. No future start is allowed after stop, but an existing attempt is not
        # declared finished without the worker's acknowledgement.
        if thread.ident is None:
            return False
        thread.join(timeout=timeout)
        alive = thread.is_alive()
        if self._error is not None:
            raise self._error
        return not alive


class CollectorRetry:
    """Wait before each collector preflight, with a stop signal that wakes the wait."""

    def __init__(
        self,
        *,
        initialize: Callable[[], None],
        resolved: Callable[[], bool],
        interval: Callable[[], float],
    ) -> None:
        self._initialize = initialize
        self._resolved = resolved
        self._interval = interval
        self._stop_requested = threading.Event()
        self.finished = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="trace-collector-retry")
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._resolved():
                if self._stop_requested.wait(self._interval()) or self._resolved():
                    return
                self._initialize()
        except BaseException as exc:
            self._error = exc
            report_no_pipeline(
                "[trace-collector-retry] worker failed: {err}", err=repr(exc), exc=exc
            )
        finally:
            self.finished.set()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def stop(self, timeout: float = 2.0) -> bool:
        """Stop admission and observe this same retry loop, including its original error."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("trace stop timeout must be finite and non-negative")
        self._stop_requested.set()
        self._thread.join(timeout=timeout)
        alive = self._thread.is_alive()
        if self._error is not None:
            raise self._error
        return not alive
