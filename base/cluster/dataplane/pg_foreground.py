"""Direct-child disposable Postgres startup and native shutdown."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import psycopg

from base.clock import SYSTEM_TIMERS, Timers
from base.cluster.dataplane.pg_plan import (
    POSTMASTER_SHUTDOWN_S,
    foreground_stop_argv,
    verify_foreground_data_dir,
)

__all__ = [
    "POSTMASTER_SHUTDOWN_S",
    "start_foreground_postgres",
    "stop_foreground_postgres",
    "wait_foreground_postgres",
]


def start_foreground_postgres(
    argv: list[str], *, log: Path, env: Mapping[str, str]
) -> subprocess.Popen[bytes]:
    """Return the direct child before readiness checks; no process-family receipts."""
    with log.open("ab", buffering=0) as output:
        return subprocess.Popen(  # noqa: S603 -- resolved postgres and private data directory
            argv, stdin=subprocess.DEVNULL, stdout=output.fileno(), stderr=output.fileno(), env=env
        )


def wait_foreground_postgres(
    process: subprocess.Popen[bytes],
    *,
    log: Path,
    port: int,
    data: Path,
    timeout_s: float = 60,
    timers: Timers = SYSTEM_TIMERS,
) -> None:
    """Accept only a live owned postmaster answering for the expected PGDATA."""
    deadline: float | None = None
    while True:
        now = timers.monotonic()
        if deadline is None:
            deadline = now + timeout_s
        if now >= deadline:
            break
        if process.poll() is not None:
            raise RuntimeError(f"foreground Postgres exited {process.returncode}: {log}")
        try:
            with psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="meiva",
                dbname="postgres",
                connect_timeout=1,
                options="-c statement_timeout=1000",
            ) as connection:
                row = connection.execute("SHOW data_directory").fetchone()
        except psycopg.OperationalError:
            timers.sleep(0.1)
            continue
        verify_foreground_data_dir(None if row is None else str(row[0]), data)
        if process.poll() is not None:
            raise RuntimeError(f"foreground Postgres exited {process.returncode}: {log}")
        return
    raise TimeoutError(f"foreground Postgres did not become ready: {log}")


def stop_foreground_postgres(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    stop_argv = foreground_stop_argv(process.args, shutdown_s=POSTMASTER_SHUTDOWN_S)
    # meiva:note
    # `stop_argv` invokes the `pg_ctl` sibling next to the launched `postgres`
    # binary against its own `-D` data directory.
    subprocess.run(  # noqa: S603
        stop_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        timeout=POSTMASTER_SHUTDOWN_S + 2,
    )
    process.wait(timeout=2)
