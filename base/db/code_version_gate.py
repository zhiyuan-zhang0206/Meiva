"""The client-side code-version gate on pooled Postgres sessions.

A runner that is offline during an update (a closed laptop) is not stopped and
its checkout does not move. When it wakes, its already-running processes keep
writing with the old code. The gate closes that hole: `deployment_state` holds
`min_code_version`, every gateway start raises it to that gateway's own code
version (`raise_min_code_version`), and every pooled session reads it and
refuses to work under a lower one.

The check rides the baseline-session restore that every pooled borrow already
runs (`base.db.connections.restore_pooled_session`): the read of the minimum is
one more column of the restore's second statement, so it adds no round trip, and
the entry-owned gate limits the read to once per `MIN_REFRESH_INTERVAL_S`.

A stale process terminates itself, and does not raise. `psycopg_pool` swallows
an `Exception` from a `check` or `configure` callback and retries until
`PoolTimeout`, and `configure` runs on a worker thread that no caller sees.
`os._exit` acts from any thread and gives the old code no chance to keep
writing during a graceful shutdown. The gate trusts the process it stops to
run this code: a process that predates the gate cannot be stopped by it (the
first release carrying it cannot deliver its own protection), and a process
holding one connection forever never re-reads the minimum. The trade-off is in
`docs/decisions/runtime/updates/execution/converge/2026-09-30-client-side-code-version-gate.md`.

Direct connections (administrator, migration applier, `pg_dump`) and explicit
targets (`connect_url`) are not gated. The `meiva` CLI is exempt by declaration
(its operator factory passes an exempt `ProcessDbGate`): `meiva stop` writes to
drain agents, and a host left behind must still be able to run it.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import NoReturn, Protocol

import psycopg

from base.host.exit_codes import CODE_BEHIND_MINIMUM_EXIT_CODE, hard_exit
from base.log import logger

# How stale the process-local copy of `min_code_version` may be. It bounds how
# long a process keeps writing after the minimum rises; it is not a lattice clock.
MIN_REFRESH_INTERVAL_S = 30.0

_REFUSAL = (
    "code version gate: process {service} runs code version {version}, below the "
    "cluster minimum {minimum} (deployment_state.min_code_version). This is stale "
    "code and must not write; exiting with code {code}. Update this host's checkout "
    "to the cluster's current commit. After an intentional rollback, lower "
    "min_code_version by hand (docs/conventions/runbook.md, Code version gate)."
)


def has_log_sink() -> bool:
    """Whether loguru has a handler, i.e. whether a record written now goes anywhere."""
    core = getattr(logger, "_core", None)
    return bool(getattr(core, "handlers", None))


def _refuse(
    version: int,
    minimum: int,
    *,
    process: str,
    on_exit: Callable[[int], NoReturn] = hard_exit,
) -> NoReturn:
    fields = {
        "service": process,
        "version": version,
        "minimum": minimum,
        "code": CODE_BEHIND_MINIMUM_EXIT_CODE,
    }
    logger.critical(_REFUSAL, **fields)
    if not has_log_sink():
        # A process that dials before it opens its sinks would otherwise exit
        # without a word: loguru discards a record no handler receives.
        sys.stderr.write(_REFUSAL.format(**fields) + "\n")
    on_exit(CODE_BEHIND_MINIMUM_EXIT_CODE)


class _Dialer(Protocol):
    """What `raise_min_code_version` needs of a `Database`; the handle's own module imports this one."""

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection: ...


def _raise_minimum(db: _Dialer, version: int) -> int:
    with db.connect(autocommit=True) as conn:
        row = conn.execute(
            "UPDATE deployment_state SET min_code_version = GREATEST(min_code_version, %s) "
            "WHERE id = 1 RETURNING min_code_version",
            (version,),
        ).fetchone()
    if row is None:
        raise RuntimeError(
            "cannot raise min_code_version: the deployment_state singleton row is missing"
        )
    minimum = int(row[0])
    logger.info(
        "code version gate: cluster minimum is {minimum} (this gateway runs {version})",
        minimum=minimum,
        version=version,
    )
    return minimum


# meiva:note
# One process's minimum-read budget, shared by every database handle.
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
        exempt: bool = False,
        on_exit: Callable[[int], NoReturn] = hard_exit,
    ) -> None:
        self._version = version
        self._process = process
        self._exempt = exempt
        self._on_exit = on_exit
        self._last_read_at: float | None = None

    # meiva:note
    # Whether this process should read the minimum in its restore SQL.
    def min_read_due(self) -> bool:
        if self._exempt:
            return False
        seen = self._last_read_at
        return seen is None or time.monotonic() - seen >= MIN_REFRESH_INTERVAL_S

    # meiva:note
    # Retain this read's time and hard-refuse the actual loaded version.
    def observe_minimum(self, minimum: int) -> None:
        self._last_read_at = time.monotonic()
        version = self._version()
        if version < minimum:
            _refuse(version, minimum, process=self._process, on_exit=self._on_exit)

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

    def raise_min_code_version(self, db: _Dialer) -> int:
        """Raise the minimum to this gateway's captured code version."""
        return _raise_minimum(db, self._version())
