from __future__ import annotations

import threading
import time
from collections.abc import Generator
from typing import Any

import psycopg
import pytest
from loguru import logger

from base.config import settings
from base.db import connections


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
    restore: Any = connections.restore_pooled_session

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
    restore: Any = connections.restore_pooled_session

    restore(conn, gate=_Gate(), budget_seconds=2.5)

    statement, _kwargs = conn.executed[0]
    assert statement == (
        "RESET ALL; SET statement_timeout = 2500; SET LOCAL transaction_timeout = 2500"
    )


def test_a_cancelled_restore_reports_one_event_and_propagates(events: list[dict[str, Any]]) -> None:
    cancelled = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    conn = _RecordingConnection(failure=cancelled)
    restore: Any = connections.restore_pooled_session

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
    restore: Any = connections.restore_pooled_session

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
    restore: Any = connections.restore_pooled_session

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
# already failed. Both timers carry the budget, so either one may end it — the
# test accepts either and requires the event to name the one that did. The
# connection is the pool's default (non-autocommit), so the restore's closing
# commit() is a real round trip here.
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
        assert isinstance(
            failures[0], (psycopg.errors.QueryCanceled, psycopg.errors.TransactionTimeout)
        ), failures
        (reported,) = [event for event in events if event.get("event") == "db_pool_restore_timeout"]
        assert (reported["name"], reported["budget_ms"]) == ("tests:restore", 1000)
        assert reported["elapsed_ms"] >= 900, "the event must carry the wait it measured"
        assert reported["ended_by"] in {"statement_timeout", "transaction_timeout"}

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
# A deferred constraint trigger that sleeps at COMMIT: the client sees exactly
# what a stalled WAL flush looks like — `commit()` waiting on the server, with
# statement_timeout unable to interrupt it (2026-10-19 review, measured 3 s under
# a 300 ms statement budget).
def _install_stalled_commit(conn: psycopg.Connection[Any]) -> None:
    conn.execute("CREATE TEMP TABLE restore_budget_stall (x int)")
    conn.execute(
        "CREATE OR REPLACE FUNCTION pg_temp.restore_budget_slow() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(3); RETURN NULL; END $$"
    )
    conn.execute(
        "CREATE CONSTRAINT TRIGGER restore_budget_slow AFTER INSERT ON restore_budget_stall "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pg_temp.restore_budget_slow()"
    )
    conn.commit()


# meiva:note
# The budget that statement_timeout cannot enforce: with a 1 s budget the stalled
# COMMIT is terminated (~1.2 s), with a 600 s budget the same commit waits the
# trigger's 3 s and succeeds. The connection dies on termination — that is what
# the pool replaces.
def test_the_transaction_budget_interrupts_a_stalled_commit(db_url: str) -> None:
    statement, _ms = connections.restore_budget_statement(1.0)
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        _install_stalled_commit(conn)
        conn.execute(statement, prepare=False)
        conn.execute("INSERT INTO restore_budget_stall VALUES (1)")
        started = time.monotonic()
        with pytest.raises(psycopg.errors.TransactionTimeout):
            conn.commit()
        assert time.monotonic() - started < 2.9, "the commit must end at the budget, not at 3 s"
        assert conn.closed, "transaction_timeout terminates the session"

    statement, _ms = connections.restore_budget_statement(600.0)
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        _install_stalled_commit(conn)
        conn.execute(statement, prepare=False)
        conn.execute("INSERT INTO restore_budget_stall VALUES (1)")
        started = time.monotonic()
        conn.commit()
        assert time.monotonic() - started >= 2.5, "without the budget the commit waits the trigger"


# meiva:note
# `SET LOCAL` is what keeps the fix safe: the budget belongs to the restore's own
# transaction. A borrower's later transaction on the same backend — longer than
# the budget — must commit normally, and the session must read back 0.
def test_a_normal_transaction_after_a_restore_is_not_terminated(db_url: str) -> None:
    restore: Any = connections.restore_pooled_session
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        restore(conn, gate=_Gate(), budget_seconds=1.0)

        conn.execute("SELECT pg_sleep(2.5)")
        row = conn.execute("SHOW transaction_timeout").fetchone()
        conn.commit()

    assert row == ("0",), "the restore's transaction budget must not outlive its transaction"
