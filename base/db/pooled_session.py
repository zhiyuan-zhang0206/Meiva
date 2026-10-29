# meiva:note
# The session a pooled backend is put into before a client uses it, and the two budgets that
# bound that work.
#
# It is one responsibility with two faces — `restore_pooled_session` and its async twin — plus
# the deadline machinery they share: a socket cut armed *outside* the wait, because psycopg
# answers a cancellation by waiting again with no bound. `base/db/connections` owns dialing and
# keeps the keepalive posture and the kwargs that compose it; the statement ceiling that bounds
# every query on a pooled backend is defined here, together with the restore that re-applies it
# after `RESET ALL`.

from __future__ import annotations

import asyncio
import time
from typing import LiteralString

import psycopg
from psycopg.rows import tuple_row

from base.db.code_version_gate import ProcessDbGate

# meiva:note
# Statement-level ceiling for every query through the sanctioned entry points
# (`base.db.connect()` / `pool()`), appended as a libpq `options` string.
# `PG_KEEPALIVE_KWARGS` (base/db/connections.py) bounds a *dead* connection (~60s to
# detect); a *live but
# wedged* one (a lock wait, a planner path gone quadratic, a table bloat) would
# otherwise hold the query for as long as Postgres lets it — with no
# statement_timeout that is unbounded. 60s is far above any production query
# here (timeline reads are single-row checkpoint fetches; the heaviest admin
# queries stay well under) and far below the keepalive detection window, so a
# hung query fails fast with a clear OperationalError instead of pinning a
# gateway/daemon request for minutes.
#
# Deliberately NOT folded into PG_KEEPALIVE_KWARGS: the migration applier
# (cli/commands/lifecycle/start/migrations.py) dials `connect(direct=True, unbounded=True)`
# and its DDL runs may legitimately exceed 60s — the migration applier must stay
# unbounded. The sanctioned entry points deliver this one ceiling — as `options`
# on a direct dial, as PG_STATEMENT_TIMEOUT_SET_SQL on a pooled one — so it is
# the single statement-timeout definition.
_STATEMENT_TIMEOUT_MS: LiteralString = "60000"
PG_STATEMENT_TIMEOUT_OPTIONS = f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}"
# meiva:note
# The same ceiling as an explicit `SET` — the delivery path that works THROUGH
# PgBouncer: the pooler drops the libpq `options` startup parameter
# (ignore_startup_parameters), and `track_extra_parameters` cannot deliver
# statement_timeout either (PgBouncer only tracks parameters Postgres reports
# to clients, GUC_REPORT, and statement_timeout is not one of them — verified
# against 1.25 source + live probe). A client-side SET is forwarded to the
# backend like any query and sticks — pgbouncer transaction pooling does NOT
# reset backend session state between clients on the ordinary release path
# (server_reset_query_always=0, the 2026-09-03 ruling: always=1 fired after
# every transaction and its DISCARD ALL wiped this client-side SET; the
# 2026-09-02 P0 pooled read-only pollution rode the pre-fix era). The uniform
# ceiling
# therefore holds because every sanctioned pooled entry point restores the
# baseline session on use (see restore_pooled_session).
# `connect()` / `pool()` issue it on every pooled dial/borrow;
# `cli/commands/data_plane/pgbouncer.py` also runs it as the pooler's `connect_query` so
# every pooled backend is bounded at birth regardless of the client's code path.
PG_STATEMENT_TIMEOUT_SET_SQL = f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}"

# meiva:note
# The session-level scrub that makes a pooled backend safe to borrow. RESET ALL
# clears every session GUC another borrower may have left behind (the 2026-09-02
# P0: a polluter's `SET default_transaction_read_only = on` on a pooled
# connection leaked onto shared backends and 500'd the message/schedule-stop
# APIs' writes) — then what RESET ALL also cleared is re-applied: the statement
# ceiling (the pooler connect_query's birth-time SET) and this connection's
# `application_name` (PgBouncer takes a client's RESET of a parameter it tracks
# as that client's own new value, and would list the connection as unnamed).
# Two statements, always together: a restore that reset without re-bounding
# would silently drop the F7 ceiling for the next borrower. The second is
# `set_config(..., false)`, `SET` as an expression, one placeholder: the name.
_PG_POOLED_SESSION_SQL: LiteralString = (
    f"set_config('statement_timeout', '{_STATEMENT_TIMEOUT_MS}', false), "
    "set_config('application_name', %s, false)"
)
PG_POOLED_BASELINE_RESTORE_SQL: tuple[LiteralString, LiteralString] = (
    "RESET ALL",
    f"SELECT {_PG_POOLED_SESSION_SQL}",
)


# meiva:note
# The scrub plus the two budgets for one restore, in a single simple-protocol
# round trip: they have to be in force before the restore touches the server,
# so they ride with the `RESET ALL`. psycopg sends a parameterless text query
# over the simple protocol, which is what allows more than one statement in one
# message; `prepare=False` pins that path whatever the connection's
# `prepare_threshold` says. Both budgets carry the same value. The transaction
# timer (Postgres 17) starts at BEGIN — a hair before the first statement — so
# in practice it is the one that ends a blown restore, and terminating the
# session is the outcome we want for a backend that stalled: the pool replaces
# it instead of reusing it. `statement_timeout` still bounds the statement on a
# connection whose transaction the timer cannot span. `SET LOCAL` reverts both
# at the restore's own COMMIT, so a borrower's later transactions on this
# backend never inherit a 5 s ceiling (checked by
# `test_a_normal_transaction_after_a_restore_is_not_terminated`). Neither
# budget reaches a backend wedged in uninterruptible I/O or a client/network
# wait; the 2026-10-18 389 s check was that class, so #5303's client-side
# deadline stays a separate item. The next statement restores the session's
# standing statement ceiling (`db_pool_restore_timeout_seconds` owns the
# value). Read lazily because this module is on the exec child's boot path
# (task #3621); `budget_seconds` overrides the configured value for one
# restore.
_DIGITS: dict[str, LiteralString] = {
    "0": "0",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
}


def restore_budget_statement(budget_seconds: float | None = None) -> tuple[LiteralString, int]:
    from base.db.config import default_restore_budget_seconds

    if budget_seconds is None:
        budget_seconds = default_restore_budget_seconds()
    budget_ms = max(1, int(budget_seconds * 1000))
    ms_lit: LiteralString = "".join(_DIGITS[ch] for ch in str(budget_ms))
    reset = PG_POOLED_BASELINE_RESTORE_SQL[0]
    statement: LiteralString = (
        f"{reset}; SET statement_timeout = {ms_lit}; SET LOCAL transaction_timeout = {ms_lit}"
    )
    return statement, budget_ms


# meiva:note
# The second statement on the borrow that also reads the cluster's minimum code
# version (`base.db.code_version_gate`): same round trip, one more column. A
# cluster with no `deployment_state` row has recorded no minimum: 0, never a refusal.
# The S608 suppression above is the format's: module constants only, no caller input.
PG_POOLED_RESTORE_WITH_MIN_SQL: LiteralString = (
    f"SELECT {_PG_POOLED_SESSION_SQL}, "  # noqa: S608
    "COALESCE((SELECT min_code_version FROM public.deployment_state WHERE id = 1), 0)"
)


# meiva:note
# The client-side deadline is the only timer that survives a backend which is not
# running: statement_timeout and transaction_timeout are enforced by the backend
# at its own CHECK_FOR_INTERRUPTS, so a stopped, black-holed or I/O-wedged backend
# leaves the borrower waiting with no timer at all (2026-10-17: 18-56 s per
# borrow). It sits strictly above the statement budget so a server timer that was
# about to fire cleanly still fires first; see
# `db_pool_restore_client_deadline_seconds` for why they must not be equal.
def restore_client_deadline_seconds(client_deadline_seconds: float | None = None) -> float:
    from base.db.config import default_restore_client_deadline_seconds

    if client_deadline_seconds is None:
        client_deadline_seconds = default_restore_client_deadline_seconds()
    return client_deadline_seconds


# meiva:note
# Raised when a restore ran past its client deadline. It is a TimeoutError so a
# caller that already treats a timed-out borrow as a retryable stall keeps doing
# so; the pool counts any check failure as a failed connection and replaces it,
# and the cut above has already made that connection unusable, so no further
# close() is attempted here.
class RestoreAbandonedError(TimeoutError):
    pass


def restore_pooled_session(
    conn: psycopg.Connection, *, gate: ProcessDbGate, budget_seconds: float | None = None
) -> None:
    # meiva:note
    # The per-borrow scrub: restore a pooled connection to its baseline session state.
    #
    # PgBouncer transaction pooling hands any backend to any client transaction
    # and never resets session state on the ordinary release path, so a borrower
    # can inherit another client's session GUCs. PgBouncer >= 1.26 isolates the
    # parameters PostgreSQL reports to clients (default_transaction_read_only,
    # the 2026-09-02 P0 vector), but unreported ones still leak — another
    # client's `search_path` fails every unqualified statement, its
    # `statement_timeout = 0` lifts the F7 ceiling — and a remote-managed data
    # plane's pooler may isolate nothing. Restoring the baseline is the
    # client-side fix: RESET ALL + the statement ceiling, run as one transaction
    # at every sanctioned pooled entry point — on a fresh dial (`connect()`), on
    # a pool backend's creation (`configure`), and on every borrow (`check`).
    # Each restore also heals the shared backend it lands on, so a polluted
    # backend stops hurting the next borrower. The same statement re-names the
    # connection and, at most every `MIN_REFRESH_INTERVAL_S`, reads the cluster's
    # minimum code version: a process below it exits here
    # (`base.db.code_version_gate`).
    #
    # Used as the psycopg_pool `configure`/`check` hook and on pooled dials:
    # PgBouncer drops the `options` startup parameter, so SQL is the one path
    # that reaches the backend. The hooks must leave the connection IDLE
    # (psycopg_pool discards a connection its callback leaves mid-transaction);
    # `commit()` ends the restore transaction and is a no-op on an autocommit
    # connection. On a dead connection the execute raises, which psycopg_pool
    # treats as a failed check — the connection is discarded and replaced, the
    # same discard path `ConnectionPool.check_connection` feeds.
    #
    # A backend that never runs cannot enforce its own timers, so this restore also
    # arms the client deadline (`restore_client_deadline_seconds`): at it, a daemon
    # thread shuts the connection's socket down (`_DeadlineCut`), the restore fails
    # with `RestoreAbandonedError`, and the pool replaces that connection.
    #
    statement, budget_ms = restore_budget_statement(budget_seconds)
    started = time.monotonic()
    try:
        conn.execute(statement, prepare=False)
        name = (gate.application_name(),)
        read_due = gate.min_read_due()
        if read_due:
            # meiva:note
            # tuple_row: a pool built with a dict row_factory must not change the shape read.
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(PG_POOLED_RESTORE_WITH_MIN_SQL, name)
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("the restore statement returned no row")
            gate.observe_minimum(int(row[2]))
        else:
            conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[1], name)
        conn.commit()
    except psycopg.errors.QueryCanceled:
        _log_restore_timeout(started, budget_ms=budget_ms, gate=gate, ended_by="statement_timeout")
        raise
    except psycopg.errors.TransactionTimeout:
        _log_restore_timeout(
            started, budget_ms=budget_ms, gate=gate, ended_by="transaction_timeout"
        )
        raise


# meiva:note
# One db_pool_restore_timeout event per restore that blew its budget, naming the
# budget that ended it: `statement_timeout` cancels the statement and leaves the
# connection usable, `transaction_timeout` terminates the session (the pool
# replaces it), and `client_deadline` is the client abandoning a restore whose
# backend never answered — the only one of the three that works while the backend
# is not running. psycopg_pool treats every one as a failed check and retries
# against its own deadline, so the borrower stops waiting out the stall.
def _log_restore_timeout(
    started: float, *, budget_ms: int, gate: ProcessDbGate, ended_by: str
) -> None:
    from base.log import logger

    logger.warning(
        "[db pool] {name} session restore exceeded its {budget_ms}ms budget after "
        "{elapsed_ms}ms ({ended_by}) — the pool counts the check as failed and "
        "retries, so this borrower does not wait out the stall",
        event="db_pool_restore_timeout",
        name=gate.application_name(),
        budget_ms=budget_ms,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        ended_by=ended_by,
    )


# meiva:note
# Unlike the sync twin, this one also arms the client-side deadline: the server's two timers run
# inside the backend, so they cannot end a wait on a backend that is stopped, black-holed or
# wedged before it next runs. The task is cancelled in this same event loop at the deadline and
# the connection is abandoned (`RestoreAbandonedError`), which the pool sees as a failed check.
async def restore_pooled_session_async(
    conn: psycopg.AsyncConnection,
    *,
    gate: ProcessDbGate,
    budget_seconds: float | None = None,
    client_deadline_seconds: float | None = None,
) -> None:
    # meiva:note
    # The async twin of the restore: `async_pool()`'s per-borrow check.
    #
    # The liveness check doubles as the baseline scrub — same RESET ALL +
    # statement ceiling, same discard-on-failure semantics (psycopg_pool replaces
    # a connection whose check raises), same name and code-version read.
    #
    statement, budget_ms = restore_budget_statement(budget_seconds)
    deadline = restore_client_deadline_seconds(client_deadline_seconds)
    started = time.monotonic()
    guard = asyncio.timeout(deadline)
    try:
        async with guard:
            await conn.execute(statement, prepare=False)
            name = (gate.application_name(),)
            read_due = gate.min_read_due()
            if read_due:
                async with conn.cursor(row_factory=tuple_row) as cur:
                    await cur.execute(PG_POOLED_RESTORE_WITH_MIN_SQL, name)
                    row = await cur.fetchone()
                if row is None:
                    raise RuntimeError("the restore statement returned no row")
                gate.observe_minimum(int(row[2]))
            else:
                await conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[1], name)
            await conn.commit()
    # meiva:note
    # Only this guard's own deadline is ours: a TimeoutError the restore raised for
    # another reason keeps its original meaning.
    except TimeoutError:
        if not guard.expired():
            raise
        _log_restore_timeout(
            started, budget_ms=int(deadline * 1000), gate=gate, ended_by="client_deadline"
        )
        raise RestoreAbandonedError(
            f"session restore abandoned after its {deadline:.3f}s client deadline"
        ) from None
    except psycopg.errors.QueryCanceled:
        _log_restore_timeout(started, budget_ms=budget_ms, gate=gate, ended_by="statement_timeout")
        raise
    except psycopg.errors.TransactionTimeout:
        _log_restore_timeout(
            started, budget_ms=budget_ms, gate=gate, ended_by="transaction_timeout"
        )
        raise
