# meiva:note
# The SQL behind a shell session's deadline: the per-agent session counter, the mandatory deadline row, and the guarded renewal with its audit row. The module imports psycopg, so `ShellSessions` loads it when a session is created or renewed and `import meiva` stays light.
# meiva:link meiva/shell/docs/shell.md
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import psycopg

from base.agents.context.clients import DatabaseHandle
from base.agents.messages.delivery.retry_io import retryable_database_error
from base.db.protocols import RestoreAbandonedError

_TTL_QUERY_ATTEMPTS = 3


def _allocated_session_id(row: tuple[int, ...] | None, agent_id: int) -> int:
    if row is None:
        raise RuntimeError(
            f"agent {agent_id} is missing or terminated — cannot allocate session_index"
        )
    return int(row[0]) - 1


def _run_with_transient_retry[T](operation: Callable[[], T]) -> T:
    for attempt in range(1, _TTL_QUERY_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= _TTL_QUERY_ATTEMPTS or not retryable_database_error(exc):
                raise
    raise AssertionError("unreachable")


# meiva:note
# Increment the per-agent session counter and return its prior value. This low-level
# primitive commits the allocation immediately for callers that need only an id;
# persistent PTY creation uses `create_ttl_fenced` instead, keeping the row lock
# through the external creation and TTL insert so termination cannot pass between
# those three steps. The connect cap of `Database.connect` keeps a black-holing
# database from hanging `meiva.shell.sessions.new()` on the OS TCP-retransmit timeout.
def next_session_index(database: DatabaseHandle, agent_id: int) -> int:
    def _allocate() -> tuple[int, ...] | None:
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute(
                "UPDATE agents_meta SET session_index = session_index + 1 "
                "WHERE id = %s AND status <> 'terminated' RETURNING session_index",
                (agent_id,),
            )
            row = cur.fetchone()
            conn.commit()
            return row

    row = _run_with_transient_retry(_allocate)
    return _allocated_session_id(row, agent_id)


# meiva:note
# Run the irreversible PTY creation behind the same uncommitted `agents_meta` write
# that allocates its id, then record its TTL before releasing that row lock. Every
# terminal transition takes that row lock before capturing its cutoff, so it either
# waits for this session to become visible and closes it below the cutoff, or wins
# first and makes this allocation refuse. A resurrection cannot reopen the row in
# between because it needs the same lock. A refused or disposed attempt still
# commits its counter advance before releasing the lock, so a stale numeric handle
# is never reused. Retry only failures before an id is allocated: after that an
# external session may exist, and a retry could create two PTYs for one request.
def create_ttl_fenced(
    database: DatabaseHandle,
    agent_id: int,
    ttl: float,
    *,
    start: Callable[[int], None],
    dispose: Callable[[], None],
) -> int:
    for attempt in range(1, _TTL_QUERY_ATTEMPTS + 1):
        allocated = False
        try:
            with database.connect() as conn, conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ WRITE")
                cur.execute(
                    "UPDATE agents_meta SET session_index = session_index + 1 "
                    "WHERE id = %s AND status <> 'terminated' RETURNING session_index",
                    (agent_id,),
                )
                row = cur.fetchone()
                session_id = _allocated_session_id(row, agent_id)
                allocated = True
                try:
                    start(session_id)
                except Exception:
                    conn.commit()
                    raise
                try:
                    with conn.transaction():
                        cur.execute(
                            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                            "VALUES (%s, %s, now() + make_interval(secs => %s))",
                            (agent_id, session_id, ttl),
                        )
                except Exception as exc:
                    dispose()
                    conn.commit()
                    raise RuntimeError(f"failed to track TTL for session {session_id}") from exc
                try:
                    conn.commit()
                except Exception:
                    dispose()
                    raise
                return session_id
        except Exception as exc:
            if allocated:
                raise
            if attempt >= _TTL_QUERY_ATTEMPTS or not retryable_database_error(exc):
                raise
    raise AssertionError("unreachable")


# meiva:note
# Writes the session's mandatory deadline row to `agent_shell_ttls`, which the TTL reaper reads (the runner role holds INSERT). Fail-loud: without the row the reaper can never reclaim the session, so the caller aborts the creation it just made. `SET TRANSACTION READ WRITE` leads the transaction so a pooled backend handed over with session-level read-only poison cannot reject the write.
def record_ttl(database: DatabaseHandle, agent_id: int, session_id: int, ttl: float) -> None:
    def _insert() -> None:
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now() + make_interval(secs => %s))",
                (agent_id, session_id, ttl),
            )
            conn.commit()

    try:
        _run_with_transient_retry(_insert)
    except Exception as exc:
        raise RuntimeError(f"failed to track TTL for session {session_id}") from exc


# meiva:note
# The guarded deadline write; None when the row is no longer unexpired. The guard compares against `clock_timestamp()`, the statement time evaluated after the row lock is acquired, never `now()` (transaction start). A renewal whose transaction began before the deadline but whose UPDATE only executes after the reaper already claimed the row must lose (issue #2053): with `now()` the guard would still pass on a session the kill was already dispatched for, and the row would show renewed while the session is dead.
def renewal_update(
    cur: psycopg.Cursor, agent_id: int, session_id: int, ttl: float
) -> datetime | None:
    cur.execute(
        "UPDATE agent_shell_ttls "
        "SET expires_at = clock_timestamp() + make_interval(secs => %s), "
        "renewals = renewals + 1, last_renewed_at = clock_timestamp() "
        "WHERE agent_id = %s AND session_id = %s AND expires_at > clock_timestamp() "
        "RETURNING expires_at",
        (ttl, agent_id, session_id),
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


# meiva:note
# The session's current deadline; None when the row is absent.
def read_expiry(database: DatabaseHandle, agent_id: int, session_id: int) -> datetime | None:
    def _select() -> tuple[datetime, ...] | None:
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT expires_at FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (agent_id, session_id),
            )
            return cur.fetchone()

    try:
        row = _run_with_transient_retry(_select)
    except (psycopg.Error, RestoreAbandonedError) as exc:
        raise RuntimeError(f"failed to renew session {session_id}") from exc
    return row[0] if row is not None else None


# meiva:note
# The guarded write: new deadline plus the audit row, one transaction (`SET TRANSACTION READ WRITE` leads it, the same pooled read-only defense as `record_ttl`). Returns the new deadline; raises ValueError when the row expired (or was reaped) between the read and this write, because the owner renews before the deadline or not at all (user ruling 2026-09-08).
def apply_renewal(
    database: DatabaseHandle, agent_id: int, session_id: int, ttl: float, prev_expires: datetime
) -> datetime:
    def _update() -> datetime | None:
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            updated = renewal_update(cur, agent_id, session_id, ttl)
            if updated is not None:
                cur.execute(
                    "INSERT INTO agent_shell_ttl_renewals "
                    "(agent_id, session_id, requested_ttl_seconds, prev_expires_at, "
                    "new_expires_at) VALUES (%s, %s, %s, %s, %s)",
                    (agent_id, session_id, ttl, prev_expires, updated),
                )
                conn.commit()
            return updated

    try:
        new_expires = _run_with_transient_retry(_update)
    except (psycopg.Error, RestoreAbandonedError) as exc:
        raise RuntimeError(f"failed to renew session {session_id}") from exc
    if new_expires is None:
        raise ValueError(
            f"session {session_id} is already past its TTL — expired sessions "
            "are reclaimed automatically; renew before the deadline"
        )
    return new_expires
