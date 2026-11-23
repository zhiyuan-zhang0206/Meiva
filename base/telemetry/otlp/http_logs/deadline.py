from __future__ import annotations

import contextlib
import os
import socket
import threading
from collections.abc import Callable
from math import inf

from base.telemetry.delivery.reporting import report_no_pipeline

_SHUTDOWN_SETTLE_SECONDS = 0.5
_WATCHER_JOIN_SECONDS = 0.1


class LogExportDeadline:
    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self.changed = threading.Condition()
        self._shutdown_at = inf

    def close_by(self, deadline: float) -> None:
        with self.changed:
            self._shutdown_at = min(self._shutdown_at, deadline - _SHUTDOWN_SETTLE_SECONDS)
            self.changed.notify_all()

    def remaining(self, deadline: float) -> float:
        with self.changed:
            return min(deadline, self._shutdown_at) - self._monotonic()


class LogExportSocketWatcher:
    def __init__(self, deadline: LogExportDeadline) -> None:
        self._deadline = deadline
        self._changed = deadline.changed
        self._request_until = inf
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._error: BaseException | None = None

    def start(self) -> None:
        with self._lock:
            if self._stop_requested.is_set():
                raise RuntimeError("OTLP log deadline watcher is stopping")
            if self._thread is not None:
                raise RuntimeError("OTLP log deadline watcher already started")
            self._thread = threading.Thread(target=self._run, daemon=True, name="otlp-log-deadline")
            thread = self._thread
        thread.start()

    def _run(self) -> None:
        try:
            self._watch()
        except BaseException as error:
            self._error = error
            report_no_pipeline(
                "OTLP log deadline watcher failed: {error}", error=repr(error), exc=error
            )
        finally:
            self._finished.set()

    def start_request(self, deadline: float) -> None:
        with self._changed:
            self._request_until = deadline

    def watch_socket(self, connection: socket.socket) -> None:
        with self._changed:
            if self._deadline.remaining(self._request_until) <= 0:
                raise TimeoutError("OTLP log export deadline expired")
            if self._socket is None:
                self._socket = socket.socket(fileno=os.dup(connection.fileno()))
                self._changed.notify_all()

    def _watch(self) -> None:
        with self._changed:
            while not self._stop_requested.is_set():
                if self._socket is None:
                    _ = self._changed.wait()
                    continue
                remaining = self._deadline.remaining(self._request_until)
                if remaining > 0:
                    _ = self._changed.wait(remaining)
                    continue
                with contextlib.suppress(OSError):
                    self._socket.shutdown(socket.SHUT_RDWR)
                self._clear_socket()

    def _clear_socket(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def finish_request(self) -> None:
        with self._changed:
            self._clear_socket()
            self._changed.notify_all()

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested.set()
        with self._changed:
            self._clear_socket()
            self._changed.notify_all()

    def stop(self, timeout: float = _WATCHER_JOIN_SECONDS) -> None:
        self.request_stop()
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        unfinished = thread.is_alive()
        if self._error is not None:
            raise self._error
        if unfinished:
            raise TimeoutError("OTLP log deadline watcher did not stop")
