# meiva:note
# The SQL behind a shell session's deadline: the per-agent session counter, the mandatory deadline row, and the guarded renewal with its audit row. The module imports psycopg, so `ShellSessions` loads it when a session is created or renewed and `import meiva` stays light.
# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import psycopg

from base.agents.context.clients import DatabaseHandle
from base.agents.messages.delivery.retry import retryable_database_error
from base.db.protocols import RestoreAbandonedError

_TTL_QUERY_ATTEMPTS = 3


def _run_with_transient_retry[T](operation: Callable[[], T]) -> T:
    for attempt in range(1, _TTL_QUERY_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= _TTL_QUERY_ATTEMPTS or not retryable_database_error(exc):
                raise
    raise AssertionError("unreachable")


# meiva:note
# Atomically increments agents_meta.session_index and returns the id to use (shared by shells and watchers); `UPDATE ... RETURNING` keeps it safe under concurrency. There is no fallback: an unavailable database, or an agent that is not in agents_meta, raises. The connect cap of `Database.connect` keeps a black-holing database from hanging `meiva.shell.sessions.new()` on the OS TCP-retransmit timeout.
def next_session_index(database: DatabaseHandle, agent_id: int) -> int:
    def _allocate() -> tuple[int, ...] | None:
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute(
                "UPDATE agents_meta SET session_index = session_index + 1 "
                "WHERE id = %s RETURNING session_index",
                (agent_id,),
            )
            row = cur.fetchone()
            conn.commit()
            return row

    row = _run_with_transient_retry(_allocate)
    if row is None:
        raise RuntimeError(
            f"agent {agent_id} not in agents_meta table — cannot allocate session_index"
        )
    return int(row[0]) - 1


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
