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
    assert statement == f"RESET ALL; SET statement_timeout = {budget_ms}"
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
    assert statement == "RESET ALL; SET statement_timeout = 2500"


def test_a_cancelled_restore_reports_one_event_and_propagates(events: list[dict[str, Any]]) -> None:
    cancelled = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    conn = _RecordingConnection(failure=cancelled)
    restore: Any = connections.restore_pooled_session
    started = time.monotonic()

    with pytest.raises(psycopg.errors.QueryCanceled):
        restore(conn, gate=_Gate())

    assert not conn.committed
    reported = [event for event in events if event.get("event") == "db_pool_restore_timeout"]
    assert len(reported) == 1
    assert reported[0]["name"] == "tests:restore"
    assert reported[0]["budget_ms"] == int(
        settings.data_plane.db_pool_restore_timeout_seconds * 1000
    )
    assert reported[0]["elapsed_ms"] >= int((time.monotonic() - started) * 1000) - 5


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
# lock stands in for the 2026-10-18 write stall: the backend is waiting on the
# server and only the statement budget can end the wait. Falsification: the same
# restore under a budget longer than the wait is still blocked while the
# short-budget one has already failed.
def test_the_budget_cancels_a_restore_stuck_on_a_locked_table(
    db_url: str, events: list[dict[str, Any]]
) -> None:
    blocker = psycopg.connect(db_url)
    blocker.execute("LOCK TABLE public.deployment_state IN ACCESS EXCLUSIVE MODE")
    conn = psycopg.connect(db_url, autocommit=True, prepare_threshold=None)
    threads: list[threading.Thread] = []
    try:
        started = time.monotonic()
        thread, failures = _restore_in_a_thread(conn, _Gate(read_due=True), budget_seconds=1.0)
        threads.append(thread)
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the restore ignored its 1s budget"
        assert time.monotonic() - started < 5.0
        assert failures and isinstance(failures[0], psycopg.errors.QueryCanceled), failures
        assert [
            (event["name"], event["budget_ms"])
            for event in events
            if event.get("event") == "db_pool_restore_timeout"
        ] == [("tests:restore", 1000)]

        thread, _failures = _restore_in_a_thread(conn, _Gate(read_due=True), budget_seconds=600.0)
        threads.append(thread)
        thread.join(timeout=1.5)
        assert thread.is_alive(), "a 600s budget cannot have ended a 1.5s wait"
    finally:
        blocker.rollback()
        for thread in threads:
            thread.join(timeout=10.0)
        conn.close()
        blocker.close()
