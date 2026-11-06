"""Durable desired revisions and live PTY command provenance for convergence."""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.daemon.schedules.claim import RunnerLiveness, read_runner_liveness
from base.db.rows import query_all, query_one
from base.db.transaction import write_transaction
from base.paths import run_dir
from base.sessions.pty import client
from base.sessions.pty.client import SessionInfo
from base.sessions.pty.paths import service_socket_path
from services.wake.schedule_manager.convergence import Desired, revision_in


@dataclass(frozen=True)
class _LockRow:
    locked: bool


@dataclass(frozen=True)
class _DesiredRow:
    enabled: bool
    status: str
    desired_revision: int
    applied_revision: int
    launch_generation: str | None
    secret_env_vars: Sequence[str]
    owner_agent_id: int | None


@dataclass(frozen=True)
class _HomeRow:
    home: str


@dataclass(frozen=True)
class _StatusRow:
    id: int
    status: str


@contextmanager
def claim_convergence(pool: ConnectionPool, schedule_id: int) -> Generator[bool, None, None]:
    """One manager owns this schedule's process effects across independent processes.

    The advisory transaction survives inner desired/applied commits but is
    released by connection failure. Desired writes remain free to advance;
    their conditional revision writes prevent this owner acknowledging newer work.
    """
    with write_transaction(pool) as conn:
        row = query_one(
            conn,
            _LockRow,
            "SELECT pg_try_advisory_xact_lock(hashtextextended('meiva.schedule-convergence:' || %s::text, 0)) AS locked",
            (schedule_id,),
        )
        assert row is not None  # noqa: S101 — SELECT always returns one boolean
        yield row.locked


def desired(
    pool: ConnectionPool, schedule_id: int, *, machine: str | None = None
) -> Desired | None:
    with pool.connection() as conn:
        if machine is not None:
            row = query_one(
                conn,
                _DesiredRow,
                "SELECT enabled, status, desired_revision, applied_revision, launch_generation, "
                "secret_env_vars, owner_agent_id FROM schedules WHERE id = %s AND machine = %s",
                (schedule_id, machine),
            )
        else:
            row = query_one(
                conn,
                _DesiredRow,
                "SELECT enabled, status, desired_revision, applied_revision, launch_generation, "
                "secret_env_vars, owner_agent_id FROM schedules WHERE id = %s",
                (schedule_id,),
            )
    if row is None:
        return None
    return Desired(
        enabled=row.enabled,
        status=row.status,
        revision=row.desired_revision,
        applied=row.applied_revision,
        launch_generation=row.launch_generation,
        secret_env_vars=tuple(row.secret_env_vars),
        owner_agent_id=row.owner_agent_id,
    )


def launched_revision(home: Path, schedule_id: int) -> int | None:
    """Read provenance from the PTY owner; missing evidence never permits replacement.

    A legacy command belongs to revision zero. The allocation generation is a
    separate release identity, still checked by the manager's exact PTY cleanup.
    """
    info = session_provenance(home, schedule_id)
    return None if info is None else revision_in(info.initial_command)


# meiva:note
# When the session started, from the owner that created it. The runner's own record carries the
# birth of the process that wrote it, so this is the bound below which that record describes the
# runner the launch replaced rather than the one it started (base/daemon/schedules/claim.py).
def launch_started_at(home: Path, schedule_id: int) -> float | None:
    info = session_provenance(home, schedule_id)
    return None if info is None else info.started_at


# meiva:note
# The PTY owner's record for this schedule's session, or None when it cannot name one.
def session_provenance(home: Path, schedule_id: int) -> SessionInfo | None:
    name = session_name(f"schedule-{schedule_id}")
    socket_path = service_socket_path(run_dir(home))
    infos = client.list_sessions(name, socket_path=socket_path, include_initial_command=True)
    return next((item for item in infos if item.name == name), None)


def mark_applied(
    pool: ConnectionPool, schedule_id: int, revision: int, *, machine: str | None = None
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
def is_active_unit(pool: ConnectionPool, machine_name: str, home: Path) -> bool:
    with pool.connection() as conn:
        rows = query_all(
            conn,
            _HomeRow,
            "SELECT home FROM machine_units WHERE machine_name = %s AND stopped_at IS NULL ORDER BY serve_gateway DESC, home ASC",
            (machine_name,),
        )
    if not rows:
        return True
    homes = [str(Path(r.home).resolve()) for r in rows]
    my_home = str(Path(home).resolve())
    return my_home == homes[0]


def close_null_runs(pool: ConnectionPool, schedule_id: int, *, note: str = "interrupted") -> None:
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE schedule_runs SET ok = false, note = %s WHERE schedule_id = %s AND ok IS NULL",
            (note, schedule_id),
        )


def claim_launch(
    pool: ConnectionPool,
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


def load_enabled(pool: ConnectionPool, machine_name: str) -> dict[int, str]:
    with pool.connection() as conn:
        rows = query_all(
            conn,
            _StatusRow,
            "SELECT id, status FROM schedules WHERE enabled AND machine = %s",
            (machine_name,),
        )
    return {row.id: row.status for row in rows}


def set_status(pool: ConnectionPool, schedule_id: int, status: str, machine_name: str) -> bool:
    with write_transaction(pool) as conn:
        return (
            conn.execute(
                "UPDATE schedules SET status=%s, updated_at=now() WHERE id=%s AND machine=%s",
                (status, schedule_id, machine_name),
            ).rowcount
            > 0
        )


def mark_running(
    pool: ConnectionPool,
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
    pool: ConnectionPool,
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


# meiva:note
# Whether the runner that should own this schedule's session is alive. A record born before the
# session started belongs to the runner a reap killed without letting it clear its record, so it
# reads as no evidence rather than as a death (2026-11-06: schedule 40 was marked lost 254 ms
# after a successful launch). Both sides of that comparison are host epoch seconds: the record's
# birth is the runner's own, the session's start is the PTY owner's.
def session_runner_liveness(home: Path, schedule_id: int, *, wall_now: float) -> RunnerLiveness:
    launched_at = launch_started_at(home, schedule_id)
    return read_runner_liveness(
        home,
        schedule_id,
        launched_at=launched_at,
        now=None if launched_at is None else wall_now,
    ).liveness
