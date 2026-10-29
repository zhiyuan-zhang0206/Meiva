from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Generator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from loguru import logger
from psycopg import sql

from base.config import settings
from base.db import pooled_session


# meiva:note
# The parts of ProcessDbGate a restore touches.
class _Gate:
    def __init__(self, *, read_due: bool = False) -> None:
        self._read_due = read_due

    def application_name(self) -> str:
        return "tests:restore"

    def min_read_due(self) -> bool:
        return self._read_due

    def observe_minimum(self, value: int) -> None:
        self.observed = value


# meiva:note
# A connection that records what the restore sends and can fail on the first
# execute.
class _RecordingConnection:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.committed = False
        self._failure = failure

    def execute(self, statement: str, params: Any = None, **kwargs: Any) -> _RecordingConnection:
        self.executed.append((statement, kwargs))
        if self._failure is not None:
            raise self._failure
        return self

    def commit(self) -> None:
        self.committed = True


@pytest.fixture
def events() -> Generator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        captured.append(dict(message.record["extra"]))

    handler = logger.add(sink, level="WARNING")
    yield captured
    logger.remove(handler)


def test_the_restore_carries_its_own_statement_budget() -> None:
    conn = _RecordingConnection()
    restore: Any = pooled_session.restore_pooled_session

    restore(conn, gate=_Gate())

    statement, kwargs = conn.executed[0]
    budget_ms = int(settings.data_plane.db_pool_restore_timeout_seconds * 1000)
    assert statement == (
        f"RESET ALL; SET statement_timeout = {budget_ms}; "
        f"SET LOCAL transaction_timeout = {budget_ms}"
    )
    assert kwargs == {"prepare": False}, (
        "the two statements must stay one simple-protocol round trip"
    )
    assert conn.committed


# meiva:note
# `budget_seconds` overrides the configured value for that one restore.
def test_one_restore_can_take_a_different_budget() -> None:
    conn = _RecordingConnection()
    restore: Any = pooled_session.restore_pooled_session

    restore(conn, gate=_Gate(), budget_seconds=2.5)

    statement, _kwargs = conn.executed[0]
    assert statement == (
        "RESET ALL; SET statement_timeout = 2500; SET LOCAL transaction_timeout = 2500"
    )


def test_a_cancelled_restore_reports_one_event_and_propagates(events: list[dict[str, Any]]) -> None:
    cancelled = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    conn = _RecordingConnection(failure=cancelled)
    restore: Any = pooled_session.restore_pooled_session

    with pytest.raises(psycopg.errors.QueryCanceled):
        restore(conn, gate=_Gate())

    assert not conn.committed
    reported = [event for event in events if event.get("event") == "db_pool_restore_timeout"]
    assert len(reported) == 1
    assert reported[0]["name"] == "tests:restore"
    assert reported[0]["budget_ms"] == int(
        settings.data_plane.db_pool_restore_timeout_seconds * 1000
    )
    assert reported[0]["elapsed_ms"] >= 0
    assert reported[0]["ended_by"] == "statement_timeout"


# meiva:note
# The other budget: a stalled COMMIT is not covered by statement_timeout, so the
# session is terminated (Postgres 17 transaction_timeout) and the event says so.
def test_a_terminated_restore_reports_one_event_and_propagates(
    events: list[dict[str, Any]],
) -> None:
    terminated = psycopg.errors.TransactionTimeout(
        "terminating connection due to transaction timeout"
    )
    conn = _RecordingConnection(failure=terminated)
    restore: Any = pooled_session.restore_pooled_session

    with pytest.raises(psycopg.errors.TransactionTimeout):
        restore(conn, gate=_Gate())

    assert not conn.committed
    (reported,) = [event for event in events if event.get("event") == "db_pool_restore_timeout"]
    assert reported["ended_by"] == "transaction_timeout"


# meiva:note
# Run one restore off the test thread, so a restore that never returns is a
# failed assertion rather than a hung test.
def _restore_in_a_thread(
    conn: psycopg.Connection[Any], gate: _Gate, *, budget_seconds: float
) -> tuple[threading.Thread, list[Exception]]:
    failures: list[Exception] = []
    restore: Any = pooled_session.restore_pooled_session

    def drive() -> None:
        try:
            restore(conn, gate=gate, budget_seconds=budget_seconds)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    return thread, failures


# meiva:note
# A restore blocked server-side ends at its budget, not when the wait ends. The
# lock stands in for a 2026-10-17 acquire: the backend is waiting on the server
# and only a budget can end the wait. Falsification: the same restore under a
# budget longer than the wait is still blocked while the short-budget one has
# already failed. Both budgets carry the same value and the transaction timer
# starts at BEGIN, a hair before the first statement, so it is the one that ends
# this wait — pinned here so a grace interval later added to the transaction
# budget turns this red. The connection is the pool's default (non-autocommit),
# so the restore's closing commit() is a real round trip here.
def test_the_budget_ends_a_restore_stuck_on_a_locked_table(
    db_url: str, events: list[dict[str, Any]]
) -> None:
    blocker = psycopg.connect(db_url)
    blocker.execute("LOCK TABLE public.deployment_state IN ACCESS EXCLUSIVE MODE")
    conn = psycopg.connect(db_url, prepare_threshold=None)
    slow_conn = psycopg.connect(db_url, prepare_threshold=None)
    threads: list[threading.Thread] = []
    try:
        started = time.monotonic()
        thread, failures = _restore_in_a_thread(conn, _Gate(read_due=True), budget_seconds=1.0)
        threads.append(thread)
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the restore ignored its 1s budget"
        assert time.monotonic() - started < 5.0
        assert failures, "the restore must fail at its budget"
        assert isinstance(failures[0], psycopg.errors.TransactionTimeout), failures
        (reported,) = [event for event in events if event.get("event") == "db_pool_restore_timeout"]
        assert (reported["name"], reported["budget_ms"]) == ("tests:restore", 1000)
        assert reported["elapsed_ms"] >= 900, "the event must carry the wait it measured"
        assert reported["ended_by"] == "transaction_timeout"

        thread, _failures = _restore_in_a_thread(
            slow_conn, _Gate(read_due=True), budget_seconds=600.0
        )
        threads.append(thread)
        thread.join(timeout=1.5)
        assert thread.is_alive(), "a 600s budget cannot have ended a 1.5s wait"
    finally:
        blocker.rollback()
        for thread in threads:
            thread.join(timeout=10.0)
        conn.close()
        slow_conn.close()
        blocker.close()


# meiva:note
# The marker is a sequence, not a row: `nextval` is not transactional, so it is the one fact that
# survives a commit the server terminated. The stalled commit sets it only by reaching the end of
# the trigger — which is exactly what "waited the trigger out" means, asserted without a clock.
def _marker_sequence(db_url: str) -> str:
    name = f"restore_budget_marker_{uuid4().hex[:10]}"
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        conn.execute(sql.SQL("CREATE SEQUENCE {}").format(sql.Identifier(name)))
    return name


def _marker_was_set(db_url: str, marker: str) -> bool:
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        row = conn.execute(
            sql.SQL("SELECT is_called FROM {}").format(sql.Identifier(marker))
        ).fetchone()
    return bool(row and row[0])


def _drop_marker(db_url: str, marker: str) -> None:
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        conn.execute(sql.SQL("DROP SEQUENCE IF EXISTS {}").format(sql.Identifier(marker)))


# meiva:note
# A deferred constraint trigger that sleeps at COMMIT: the client sees exactly
# what a stalled WAL flush looks like — `commit()` waiting on the server, with
# statement_timeout unable to interrupt it (2026-10-19 review, measured 3 s under
# a 300 ms statement budget). Its last act marks `marker`.
def _install_stalled_commit(conn: psycopg.Connection[Any], *, marker: str) -> None:
    conn.execute("CREATE TEMP TABLE restore_budget_stall (x int)")
    conn.execute(
        sql.SQL(
            "CREATE OR REPLACE FUNCTION pg_temp.restore_budget_slow() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(3); PERFORM nextval({}); "
            "RETURN NULL; END $$"
        ).format(sql.Literal(marker))
    )
    conn.execute(
        "CREATE CONSTRAINT TRIGGER restore_budget_slow AFTER INSERT ON restore_budget_stall "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pg_temp.restore_budget_slow()"
    )
    conn.commit()


# meiva:note
# The budget this test hands the restore, the one it hands the second run, and the guard against a
# hung test. The guard is derived from the budget so it cannot drift away from it, and 4x is the
# margin: room for a slow machine, still a failure inside a suite timeout.
_BUDGET_S = 1.0
_SLOW_BUDGET_S = 600.0
_HANG_GUARD_S = _BUDGET_S * 4


# meiva:note
# A guard below the budget would let a test that never reaches the cut still pass.
def test_the_hang_guard_sits_above_the_budget() -> None:
    assert _HANG_GUARD_S > _BUDGET_S


# meiva:note
# The budget that statement_timeout cannot enforce: with a 1 s budget the stalled
# COMMIT is terminated (~1.2 s), with a 600 s budget the same commit waits the
# trigger's 3 s and succeeds. The connection dies on termination — that is what
# the pool replaces.
#
# The judge is the trigger's own marker, not the clock: a commit the budget ended never reaches
# `nextval`, and the unbudgeted one must. Those two facts say "the stall was cut short at the
# budget" without measuring how busy the machine was; the numbers below are a hang guard.
def test_the_transaction_budget_interrupts_a_stalled_commit(db_url: str) -> None:
    marker = _marker_sequence(db_url)
    try:
        statement, _ms = pooled_session.restore_budget_statement(_BUDGET_S)
        with psycopg.connect(db_url, prepare_threshold=None) as conn:
            _install_stalled_commit(conn, marker=marker)
            conn.execute(statement, prepare=False)
            conn.execute("INSERT INTO restore_budget_stall VALUES (1)")
            started = time.monotonic()
            with pytest.raises(psycopg.errors.TransactionTimeout):
                conn.commit()
            spent = time.monotonic() - started
            assert conn.closed, "transaction_timeout terminates the session"
        assert not _marker_was_set(db_url, marker), (
            "the budget must end the commit short of the trigger's end"
        )
        # meiva:note
        # Guard against a hung test; the verdict is the marker above, and this bound is derived
        # from the budget (see `_HANG_GUARD_S`) so it follows a change to it.
        assert spent < _HANG_GUARD_S, "guard against a hung test; the verdict is the marker"

        statement, _ms = pooled_session.restore_budget_statement(_SLOW_BUDGET_S)
        with psycopg.connect(db_url, prepare_threshold=None) as conn:
            _install_stalled_commit(conn, marker=marker)
            conn.execute(statement, prepare=False)
            conn.execute("INSERT INTO restore_budget_stall VALUES (1)")
            started = time.monotonic()
            conn.commit()
            assert time.monotonic() - started >= 2.5, (
                "without the budget the commit waits the trigger"
            )
        assert _marker_was_set(db_url, marker), "an unbudgeted commit must reach the trigger's end"
    finally:
        _drop_marker(db_url, marker)


# meiva:note
# `SET LOCAL` is what keeps the fix safe: the budget belongs to the restore's own
# transaction. A borrower's later transaction on the same backend — longer than
# the budget — must commit normally, and the session must read back 0.
def test_a_normal_transaction_after_a_restore_is_not_terminated(db_url: str) -> None:
    restore: Any = pooled_session.restore_pooled_session
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        restore(conn, gate=_Gate(), budget_seconds=1.0)

        conn.execute("SELECT pg_sleep(2.5)")
        row = conn.execute("SHOW transaction_timeout").fetchone()
        conn.commit()

    assert row == ("0",), "the restore's transaction budget must not outlive its transaction"


# meiva:note
# A connection whose restore never gets an answer: the async twin of a backend
# that is stopped, black-holed or wedged before it next runs. No server timer can
# end this wait — only the client deadline can.
class _SilentAsyncConnection:
    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, statement: str, params: Any = None, **kwargs: Any) -> None:
        self.executed += 1
        await asyncio.sleep(3600)


# meiva:note
# The deadline is the client's own timer, and it reports as such: a restore that
# outlives it raises RestoreAbandonedError (a TimeoutError, so existing retry-on-stall
# callers keep working) and logs one event naming `client_deadline` — the
# distinction the 2026-10-17 investigation needed and did not have.
async def test_the_client_deadline_abandons_a_restore_that_is_never_answered(
    events: list[dict[str, Any]],
) -> None:
    conn = _SilentAsyncConnection()
    restore: Any = pooled_session.restore_pooled_session_async

    started = time.monotonic()
    with pytest.raises(pooled_session.RestoreAbandonedError):
        await restore(conn, gate=_Gate(), budget_seconds=30.0, client_deadline_seconds=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, "the client deadline must end the wait, not the test timeout"
    assert conn.executed == 1
    (reported,) = [event for event in events if event.get("event") == "db_pool_restore_timeout"]
    assert reported["ended_by"] == "client_deadline"
    assert reported["budget_ms"] == 200
    assert reported["elapsed_ms"] >= 100


# meiva:note
# Falsification for the guard: a TimeoutError raised by the restore itself is not
# this deadline's verdict and must keep its own meaning (no event, no rebranding).
async def test_a_timeout_raised_by_the_restore_keeps_its_own_meaning(
    events: list[dict[str, Any]],
) -> None:
    class _Raising(_SilentAsyncConnection):
        async def execute(self, statement: str, params: Any = None, **kwargs: Any) -> None:
            raise TimeoutError("something else timed out")

    restore: Any = pooled_session.restore_pooled_session_async

    with pytest.raises(TimeoutError) as caught:
        await restore(_Raising(), gate=_Gate(), budget_seconds=30.0, client_deadline_seconds=5.0)

    assert not isinstance(caught.value, pooled_session.RestoreAbandonedError)
    assert [event for event in events if event.get("event") == "db_pool_restore_timeout"] == []
