"""Durable desired revisions and live PTY command provenance for convergence."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.db.transaction import write_transaction
from base.paths import run_dir
from base.sessions.pty import client
from base.sessions.pty.paths import service_socket_path
from services.wake.schedule_manager.convergence import Desired, revision_in


@contextmanager
def claim_convergence(pool: ConnectionPool[Any], schedule_id: int) -> Generator[bool, None, None]:
    """One manager owns this schedule's process effects across independent processes.

    The advisory transaction survives inner desired/applied commits but is
    released by connection failure. Desired writes remain free to advance;
    their conditional revision writes prevent this owner acknowledging newer work.
    """
    with write_transaction(pool) as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('meiva.schedule-convergence:' || %s::text, 0))",
            (schedule_id,),
        ).fetchone()
        assert row is not None  # noqa: S101 — SELECT always returns one boolean
        yield bool(row[0])


def desired(
    pool: ConnectionPool[Any], schedule_id: int, *, machine: str | None = None
) -> Desired | None:
    with pool.connection() as conn:
        if machine is not None:
            row = conn.execute(
                "SELECT enabled, status, desired_revision, applied_revision, launch_generation "
                "FROM schedules WHERE id = %s AND machine = %s",
                (schedule_id, machine),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT enabled, status, desired_revision, applied_revision, launch_generation "
                "FROM schedules WHERE id = %s",
                (schedule_id,),
            ).fetchone()
    return None if row is None else Desired(*row)


def launched_revision(home: Path, schedule_id: int) -> int | None:
    """Read provenance from the PTY owner; missing evidence never permits replacement.

    A legacy command belongs to revision zero. The allocation generation is a
    separate release identity, still checked by the manager's exact PTY cleanup.
    """
    name = session_name(f"schedule-{schedule_id}")
    socket_path = service_socket_path(run_dir(home))
    infos = client.list_sessions(name, socket_path=socket_path, include_initial_command=True)
    info = next((item for item in infos if item.name == name), None)
    return None if info is None else revision_in(info.initial_command)


def mark_applied(
    pool: ConnectionPool[Any], schedule_id: int, revision: int, *, machine: str | None = None
) -> None:
    with write_transaction(pool) as conn:
        if machine is not None:
            conn.execute(
                "UPDATE schedules SET applied_revision = %s WHERE id = %s AND machine = %s AND desired_revision = %s",
                (revision, schedule_id, machine, revision),
            )
        else:
            conn.execute(
                "UPDATE schedules SET applied_revision = %s WHERE id = %s AND desired_revision = %s",
                (revision, schedule_id, revision),
            )
