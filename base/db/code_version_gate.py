# meiva:note
# Process database gate on pooled Postgres sessions.
#
# Direct connections (administrator, migration applier, `pg_dump`) and explicit
# targets (`connect_url`) are not gated. The `meiva` CLI is exempt by declaration
# (its operator factory passes an exempt `ProcessDbGate`): `meiva stop` writes to
# drain agents, and a host left behind must still be able to run it.
#

from __future__ import annotations

import time
from collections.abc import Callable
from typing import NoReturn

# meiva:note
# How stale the process-local copy of the refresh interval may be.
MIN_REFRESH_INTERVAL_S = 30.0


# meiva:note
# One process's database identity, shared by every database handle.
#
# The entry point supplies its captured-code reader and gate posture. Config
# refreshes, connection rebuilds and independent pools retain this same owner.
# Due/read observation intentionally remains two-phase: concurrent first
# restores may both read the minimum, as they did with the process timestamp.
class ProcessDbGate:
    def __init__(
        self,
        *,
        version: Callable[[], int],
        process: str,
        end_process: Callable[[int], NoReturn],
        exempt: bool = False,
    ) -> None:
        self._version = version
        self._process = process
        self._end_process = end_process
        self._exempt = exempt
        self._last_read_at: float | None = None

    # meiva:note
    # Whether this process should read the minimum in its restore SQL.
    def min_read_due(self) -> bool:
        if self._exempt:
            return False
        seen = self._last_read_at
        return seen is None or time.monotonic() - seen >= MIN_REFRESH_INTERVAL_S

    # meiva:note
    # Retain this read's time.
    def observe_minimum(self, minimum: int) -> None:
        del minimum
        self._last_read_at = time.monotonic()

    # meiva:note
    # Shift the recorded read timestamp backward so tests can advance the refresh window without patching `time`.
    def age_last_read(self, seconds: float) -> None:
        if self._last_read_at is not None:
            self._last_read_at -= seconds

    def application_name(self) -> str:
        """Name the process image; an exempt CLI never resolves a Git version."""
        if self._exempt:
            return "meiva:cli"
        return f"meiva:{self._process}:v{self._version()}"
