# meiva:note
# `base.db` connection helpers read settings once and hand back a working connection / pool,
# so call sites stop hand-writing `psycopg.connect(settings.data_plane.db_url)`.
# These pin that the helpers read the live settings URL and pass options through.
# The tests also pin the database identity and statement timeout attached to sessions.

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, NoReturn

import psycopg
import pytest

from base import db
from base.config import settings
from base.db import Database, connections, pooled_session
from base.db import code_version_gate as gate
from base.host.exit_codes import hard_exit


def _session_direct_url(_config: object = None, **_kwargs: object) -> connections.DirectResolution:
    return connections.DirectResolution(url=settings.data_plane.db_url, is_direct=True)


def test_connect_runs_a_query(
    db_url: str,
    process_gate: gate.ProcessDbGate,
) -> None:
    with db.connect(gate=process_gate) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_connect_autocommit_passthrough(db_url: str, process_gate: gate.ProcessDbGate) -> None:
    with db.connect(autocommit=True, gate=process_gate) as conn:
        assert conn.autocommit is True


def test_connect_defaults_to_manual_commit(
    db_url: str,
    process_gate: gate.ProcessDbGate,
) -> None:
    with db.connect(gate=process_gate) as conn:
        assert conn.autocommit is False


def test_connect_url_bounds_statements_unless_unbounded(db_url: str) -> None:
    """Against a real Postgres named explicitly: the door delivers the 60s
    statement ceiling by default and none when the caller dials unbounded."""
    url = settings.data_plane.db_url
    with db.connect_url(url) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone() == ("1min",)
    with db.connect_url(url, autocommit=True, unbounded=True) as conn:
        assert conn.autocommit is True
        assert conn.execute("SHOW statement_timeout").fetchone() == ("0",)


def test_pool_hands_out_working_connections(
    db_url: str,
    process_gate: gate.ProcessDbGate,
) -> None:
    pool = db.pool(gate=process_gate, min_size=1, max_size=2)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
    finally:
        pool.close()


# ── the code-version gate ────────────────────────────────────────────────────

_VERSION = 500
_PROCESS = "db-test"


class _Exited(BaseException):
    # meiva:note
    # What the test's exit raises: the process would have ended here, and the code it
    # asked for is on the exception.
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@pytest.fixture
def _gated_process() -> list[int]:
    # meiva:note
    # The exit codes this process was asked to leave with. The gate takes the exit it
    # ends through, so the test hands it a recorder and no global is rewritten: the
    # loguru sinks and the stdlib handlers `hard_exit` closes stay the session's.
    return []


def _exit_raised(exits: list[int]) -> Callable[[int], NoReturn]:
    # meiva:note
    # An `end_process` that records the code and raises `_Exited` where a live process
    # would call `os._exit`.
    def end_process(code: int) -> NoReturn:
        exits.append(code)
        raise _Exited(code)

    return end_process


@pytest.fixture
def process_gate(_gated_process: list[int]) -> gate.ProcessDbGate:
    """The test process's explicit budget, with the original fixed version and identity."""
    return gate.ProcessDbGate(
        version=lambda: _VERSION, process=_PROCESS, end_process=_exit_raised(_gated_process)
    )


# ── the read schedule ────────────────────────────────────────────────────────


def test_the_first_borrow_reads_and_the_next_thirty_seconds_do_not(
    process_gate: gate.ProcessDbGate,
) -> None:
    assert process_gate.min_read_due() is True
    process_gate.observe_minimum(0)
    assert process_gate.min_read_due() is False
    process_gate.age_last_read(gate.MIN_REFRESH_INTERVAL_S - 5.0)
    assert process_gate.min_read_due() is False
    process_gate.age_last_read(5.0)
    assert process_gate.min_read_due() is True


def test_an_exempt_process_never_reads_the_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    process_gate = gate.ProcessDbGate(
        process="cli",
        version=lambda: pytest.fail("the CLI needs no version"),
        exempt=True,
        end_process=hard_exit,
    )
    assert process_gate.min_read_due() is False


# ── the verdict ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("minimum", [0, _VERSION - 1, _VERSION, _VERSION + 10])
def test_a_process_observes_minimum_without_exiting(
    process_gate: gate.ProcessDbGate, minimum: int, _gated_process: list[int]
) -> None:
    process_gate.observe_minimum(minimum)
    assert _gated_process == []


# ── the restore statement schedule (fake connection) ─────────────────────────


class _Cursor:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...] = ()) -> _Cursor:
        self.conn.executed.append((sql, params))
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return ("60000", self.conn.executed[-1][1][0], self.conn.minimum)


class _FakeConn:
    """Records every statement with its parameters, and answers the combined read
    with `minimum`."""

    def __init__(self, minimum: int) -> None:
        self.minimum = minimum
        self.executed: list[tuple[str, tuple[str, ...]]] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple[str, ...] = (), **_kwargs: object) -> None:
        self.executed.append((sql, params))

    def cursor(self, **_kwargs: object) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1


def test_the_restore_reads_the_minimum_at_most_every_interval(
    process_gate: gate.ProcessDbGate,
) -> None:
    """Two statements per borrow, always; the second is the plain statement ceiling
    except on the borrow that also reads the minimum."""
    conn = _FakeConn(minimum=0)
    restore: Any = pooled_session.restore_pooled_session

    restore(conn, gate=process_gate)
    restore(conn, gate=process_gate)
    process_gate.age_last_read(gate.MIN_REFRESH_INTERVAL_S)
    restore(conn, gate=process_gate)

    name = (process_gate.application_name(),)
    budget, _ms = pooled_session.restore_budget_statement()
    _reset, plain = pooled_session.PG_POOLED_BASELINE_RESTORE_SQL
    combined = pooled_session.PG_POOLED_RESTORE_WITH_MIN_SQL
    assert conn.executed == [
        (budget, ()),
        (combined, name),
        (budget, ()),
        (plain, name),
        (budget, ()),
        (combined, name),
    ]
    assert conn.commits == 3


class _AsyncCursor(_Cursor):
    async def __aenter__(self) -> _AsyncCursor:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, sql: str, params: tuple[str, ...] = ()) -> _AsyncCursor:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.conn.executed.append((sql, params))
        return self

    async def fetchone(self) -> tuple[Any, ...] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        return ("60000", self.conn.executed[-1][1][0], self.conn.minimum)


class _FakeAsyncConn(_FakeConn):
    async def execute(self, sql: str, params: tuple[str, ...] = (), **_kwargs: object) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.executed.append((sql, params))

    def cursor(self, **_kwargs: object) -> _AsyncCursor:  # pyright: ignore[reportIncompatibleMethodOverride]
        return _AsyncCursor(self)

    async def commit(self) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.commits += 1


async def test_the_async_restore_follows_the_same_schedule(
    process_gate: gate.ProcessDbGate, _gated_process: list[int]
) -> None:
    conn = _FakeAsyncConn(minimum=0)
    restore: Any = pooled_session.restore_pooled_session_async

    await restore(conn, gate=process_gate)
    await restore(conn, gate=process_gate)
    process_gate.age_last_read(gate.MIN_REFRESH_INTERVAL_S)
    await restore(conn, gate=process_gate)

    name = (process_gate.application_name(),)
    budget, _ms = pooled_session.restore_budget_statement()
    _reset, plain = pooled_session.PG_POOLED_BASELINE_RESTORE_SQL
    combined = pooled_session.PG_POOLED_RESTORE_WITH_MIN_SQL
    assert conn.executed == [
        (budget, ()),
        (combined, name),
        (budget, ()),
        (plain, name),
        (budget, ()),
        (combined, name),
    ]


def test_an_exempt_process_keeps_the_plain_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    process_gate = gate.ProcessDbGate(
        process="cli",
        version=lambda: pytest.fail("the CLI needs no version"),
        exempt=True,
        end_process=hard_exit,
    )
    conn = _FakeConn(minimum=10**9)
    restore: Any = pooled_session.restore_pooled_session

    restore(conn, gate=process_gate)

    budget, _ms = pooled_session.restore_budget_statement()
    _reset, plain = pooled_session.PG_POOLED_BASELINE_RESTORE_SQL
    assert conn.executed == [(budget, ()), (plain, ("meiva:cli",))]


# ── real Postgres ────────────────────────────────────────────────────────────


@pytest.fixture
def stored_minimum(db_conn: None) -> Iterator[int]:
    """The suite database's current `min_code_version`, restored when the test ends."""
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        row = conn.execute("SELECT min_code_version FROM deployment_state WHERE id = 1").fetchone()
        assert row is not None
        original = int(row[0])
        try:
            yield original
        finally:
            conn.execute("UPDATE deployment_state SET min_code_version = %s", (original,))


def _set_minimum(value: int) -> None:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute("UPDATE deployment_state SET min_code_version = %s", (value,))


def test_a_pooled_dial_at_the_stored_minimum_works_and_keeps_the_ceiling(
    process_gate: gate.ProcessDbGate, stored_minimum: int, _gated_process: list[int]
) -> None:
    """The combined statement is `SET statement_timeout` as an expression: after the
    read, the session carries the same 60s ceiling the plain restore gives."""
    _set_minimum(_VERSION)
    with db.connect(gate=process_gate) as conn:
        row = conn.execute("SHOW statement_timeout").fetchone()
        assert row is not None and row[0] == "1min"
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == f"meiva:{_PROCESS}:v{_VERSION}"
    assert _gated_process == []


def test_a_dict_row_pool_borrows_through_the_gate(
    process_gate: gate.ProcessDbGate, stored_minimum: int, _gated_process: list[int]
) -> None:
    """The read builds its own tuple cursor, so a pool whose connections return
    dicts is not broken by it."""
    from psycopg.rows import dict_row

    _set_minimum(_VERSION)
    pool = db.pool(gate=process_gate, min_size=1, max_size=1, row_factory=dict_row, autocommit=True)
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT 1 AS one").fetchone()
        assert row == {"one": 1}
    finally:
        pool.close()
    assert _gated_process == []


async def test_the_async_restore_reads_the_stored_minimum(
    process_gate: gate.ProcessDbGate,
    stored_minimum: int,
    _gated_process: list[int],
) -> None:
    _set_minimum(_VERSION)
    restore: Any = pooled_session.restore_pooled_session_async
    async with await psycopg.AsyncConnection.connect(settings.data_plane.db_url) as aconn:
        await restore(aconn, gate=process_gate)
        cur = await aconn.execute("SHOW statement_timeout")
        row = await cur.fetchone()
        assert row is not None and row[0] == "1min"
    assert _gated_process == []


def test_direct_dials_are_not_gated_and_carry_no_process_name(
    process_gate: gate.ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
    stored_minimum: int,
    _gated_process: list[int],
) -> None:
    _set_minimum(_VERSION + 1)
    with db.connect(direct=True, gate=process_gate, direct_url=_session_direct_url) as conn:
        params = conn.info.get_parameters()
        assert not params.get("application_name", "").startswith("meiva:")
    assert _gated_process == []


def test_an_exempt_process_dials_as_the_cli_without_a_version(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int, _gated_process: list[int]
) -> None:
    _set_minimum(_VERSION + 1)
    process_gate = gate.ProcessDbGate(
        process="cli",
        version=lambda: pytest.fail("the CLI needs no version"),
        exempt=True,
        end_process=hard_exit,
    )

    with db.connect(gate=process_gate) as conn:
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == "meiva:cli"
    assert _gated_process == []


def test_independent_handles_share_one_process_budget(
    stored_minimum: int,
    _gated_process: list[int],
) -> None:
    owner = gate.ProcessDbGate(
        version=lambda: _VERSION,
        process="test-process",
        end_process=_exit_raised(_gated_process),
    )
    _set_minimum(_VERSION)
    first = Database.from_settings(gate=owner)
    second = Database.from_settings(gate=owner)
    with first.connect() as conn:
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == f"meiva:test-process:v{_VERSION}"
    with second.connect() as conn:
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == f"meiva:test-process:v{_VERSION}"
    with Database.from_settings(gate=owner).connect() as conn:
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == f"meiva:test-process:v{_VERSION}"
    assert _gated_process == []


async def test_sync_and_async_pool_callbacks_retain_the_same_gate(
    stored_minimum: int,
    _gated_process: list[int],
) -> None:
    from psycopg_pool import AsyncConnectionPool

    owner = gate.ProcessDbGate(
        version=lambda: _VERSION,
        process="test-process",
        end_process=_exit_raised(_gated_process),
    )
    _set_minimum(_VERSION)
    first = Database.from_settings(gate=owner).pool(min_size=1, max_size=1)
    second = Database.from_settings(gate=owner).async_pool(
        AsyncConnectionPool, min_size=1, max_size=1, timeout=5
    )
    try:
        await second.open(wait=True)
        with first.connection() as conn:
            row = conn.execute("SHOW application_name").fetchone()
            assert row is not None and row[0] == f"meiva:test-process:v{_VERSION}"
        async with second.connection() as aconn:
            cur = await aconn.execute("SHOW application_name")
            row = await cur.fetchone()
            assert row is not None and row[0] == f"meiva:test-process:v{_VERSION}"
    finally:
        await second.close()
        first.close()
    other = gate.ProcessDbGate(
        version=lambda: _VERSION, process="other-process", end_process=hard_exit
    )
    assert other.min_read_due() is True


def test_exempt_process_owner_never_needs_a_loaded_code_version() -> None:
    def no_version() -> int:
        raise AssertionError("an exempt CLI must not resolve Git")

    owner = gate.ProcessDbGate(
        version=no_version, process="cli", exempt=True, end_process=hard_exit
    )
    assert owner.application_name() == "meiva:cli"
    assert owner.min_read_due() is False
