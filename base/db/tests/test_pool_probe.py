from __future__ import annotations

from typing import LiteralString

import psycopg
import pytest

from base.db.tests.pool_probe import PoolProbe
from base.db.tests.pool_probe import pool_probe as pool_probe


async def test_an_existing_foreign_backend_does_not_belong_to_the_closed_pool(
    db_url: str, pool_probe: PoolProbe
) -> None:
    with psycopg.connect(db_url, application_name="meiva:foreign-baseline") as foreign:
        probe = PoolProbe(pool_probe.observer, pool_probe.gate)
        pool = probe.database().pool(min_size=0, max_size=1)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
        finally:
            pool.close()
        await probe.wait_released(10.0)
        rows = probe.rows()
        assert rows, "the old global count still sees the foreign baseline"
        assert any(row.pid == foreign.info.backend_pid for row in rows)
        assert not probe.owned(rows)


async def test_a_new_foreign_leak_still_fails_the_independent_global_guard(
    db_url: str, pool_probe: PoolProbe
) -> None:
    with psycopg.connect(db_url, application_name="meiva:new-foreign-control"):
        pool_probe.assert_owned_released()
        with pytest.raises(AssertionError, match="new global connections remain"):
            pool_probe.assert_global_restored()
    await pool_probe.wait_released(10.0)


async def test_a_closed_pool_with_a_held_owned_borrow_still_fails_until_returned(
    pool_probe: PoolProbe,
) -> None:
    pool = pool_probe.database().pool(min_size=0, max_size=1)
    conn = pool.getconn()
    try:
        conn.execute("SELECT 'synthetic-private-value'")
        diagnostic = pool_probe.diagnostics(pool_probe.rows())
        assert "synthetic-private-value" not in diagnostic
        assert "wait_event_type" in diagnostic and "backend_start" in diagnostic
        with pytest.raises(AssertionError, match="owned pool connections remain"):
            pool_probe.assert_owned_released()
        pool.close()
        assert pool.closed
        with pytest.raises(AssertionError, match="owned pool connections remain"):
            pool_probe.assert_owned_released()
    finally:
        pool.putconn(conn)
        pool.close()
    await pool_probe.wait_released(10.0)


@pytest.mark.parametrize(
    ("query", "invalid"),
    [
        ("SELECT 'synthetic-private-value'", False),
        ("SELECT $$synthetic-private-value$$", False),
        ("SELECT $tag$synthetic-private-value$tag$", False),
        (r"SELECT E'synthetic-private-value\'still-private'", False),
        ("SELECT $π$synthetic-private-value$π$", False),
        ("SELECT 1 /* outer /* inner */ synthetic-private-value */", False),
        ("SELECT 'synthetic-private-value", True),
    ],
)
async def test_diagnostics_never_read_or_echo_sql_statement_text(
    pool_probe: PoolProbe, query: LiteralString, invalid: bool
) -> None:
    pool = pool_probe.database().pool(min_size=0, max_size=1)
    try:
        with pool.connection() as conn:
            if invalid:
                with pytest.raises(psycopg.errors.SyntaxError):
                    conn.execute(query)
            else:
                conn.execute(query)
            rows = pool_probe.rows()
            assert pool_probe.owned(rows)
            assert all(row.query == "unknown (query omitted)" for row in rows)
            with pytest.raises(AssertionError, match="owned pool connections remain") as raised:
                pool_probe.assert_owned_released()
            assert "private" not in str(raised.value)
            conn.rollback()
    finally:
        pool.close()
    await pool_probe.wait_released(10.0)
