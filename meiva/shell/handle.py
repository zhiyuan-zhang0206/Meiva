"""What a launched background command or watcher gives back to the caller."""

from __future__ import annotations

from typing import NamedTuple

__all_for_meiva__ = ["SessionHandle"]

__all__ = ["SessionHandle"]


class SessionHandle(NamedTuple):
    """A command or watcher running in one of your sessions: the session id and
    the log file its output streams to.

    `run_background` and the watcher entries (`launch`, `at`, `cron`) return the
    same handle. `sessions` accepts the handle itself or its `session_id`; read
    `output_path` for the output log.
    """

    session_id: int
    output_path: str
