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
                "SELECT enabled, status, desired_revision, applied_revision, launch_generation, secret_env_vars "
                "FROM schedules WHERE id = %s AND machine = %s",
                (schedule_id, machine),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT enabled, status, desired_revision, applied_revision, launch_generation, secret_env_vars "
                "FROM schedules WHERE id = %s",
                (schedule_id,),
            ).fetchone()
    if row is None:
        return None
    return Desired(
        enabled=bool(row[0]),
        status=row[1],
        revision=row[2],
        applied=row[3],
        launch_generation=row[4],
        secret_env_vars=tuple(row[5] or ()),
    )


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


# meiva:note
# Co-located split-unit tie-breaker: if machine_units has active rows
# for machine_name, the elected unit (serve_gateway=True or lowest home path)
# is active; stopped or un-elected units yield. When machine_units has no active rows
# (e.g. standalone tests), proceed normally.
def is_active_unit(pool: ConnectionPool[Any], machine_name: str, home: Path) -> bool:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT home FROM machine_units WHERE machine_name = %s AND stopped_at IS NULL ORDER BY serve_gateway DESC, home ASC",
            (machine_name,),
        ).fetchall()
    if not rows:
        return True
    homes = [str(Path(r[0]).resolve()) for r in rows]
    my_home = str(Path(home).resolve())
    return my_home == homes[0]


def close_null_runs(
    pool: ConnectionPool[Any], schedule_id: int, *, note: str = "interrupted"
) -> None:
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE schedule_runs SET ok = false, note = %s WHERE schedule_id = %s AND ok IS NULL",
            (note, schedule_id),
        )


def claim_launch(
    pool: ConnectionPool[Any],
    schedule_id: int,
    revision: int,
    epoch: str | None,
    machine_name: str,
) -> bool:
    with write_transaction(pool) as conn:
        return (
            conn.execute(
                "UPDATE schedules SET launch_count = 1, applied_revision = %s, "
                "launch_generation = %s, status = 'error', last_error = %s WHERE id = %s "
                "AND machine = %s AND enabled AND desired_revision = %s AND launch_count = 0",
                (
                    revision,
                    epoch,
                    "Launch outcome unknown; inspect before explicitly restarting",
                    schedule_id,
                    machine_name,
                    revision,
                ),
            ).rowcount
            == 1
        )


def load_enabled(pool: ConnectionPool[Any], machine_name: str) -> dict[int, str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, status FROM schedules WHERE enabled AND machine = %s",
            (machine_name,),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def set_status(pool: ConnectionPool[Any], schedule_id: int, status: str, machine_name: str) -> bool:
    with write_transaction(pool) as conn:
        return (
            conn.execute(
                "UPDATE schedules SET status=%s, updated_at=now() WHERE id=%s AND machine=%s",
                (status, schedule_id, machine_name),
            ).rowcount
            > 0
        )


def mark_running(
    pool: ConnectionPool[Any],
    schedule_id: int,
    revision: int,
    epoch: str | None,
    machine_name: str,
) -> None:
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE schedules SET applied_revision = %s, "
            "launch_generation = COALESCE(%s, launch_generation), status = CASE "
            "WHEN applied_revision = %s AND status = 'completed' THEN status ELSE 'running' END, "
            "last_error = NULL WHERE id = %s AND machine = %s AND desired_revision = %s AND enabled",
            (revision, epoch, revision, schedule_id, machine_name, revision),
        )


def mark_lost(
    pool: ConnectionPool[Any],
    schedule_id: int,
    revision: int,
    machine_name: str,
) -> bool:
    with write_transaction(pool) as conn:
        return (
            conn.execute(
                "UPDATE schedules SET status = 'error', last_error = %s, updated_at = now() "
                "WHERE id = %s AND machine = %s AND desired_revision = %s AND applied_revision = %s "
                "AND enabled AND status = 'running'",
                (
                    "Schedule process lost; inspect effects and explicitly restart to run again",
                    schedule_id,
                    machine_name,
                    revision,
                    revision,
                ),
            ).rowcount
            > 0
        )
