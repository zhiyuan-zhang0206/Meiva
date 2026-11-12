from __future__ import annotations

import psycopg
import pytest

from base.db.tests.pool_probe import PoolProbe, query_shape
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
    "query",
    [
        "SELECT 'synthetic-private-value'",
        "SELECT $$synthetic-private-value$$",
        "SELECT $tag$synthetic-private-value$tag$",
        r"SELECT E'synthetic-private-value\'still-private'",
    ],
)
def test_diagnostics_keep_sql_shape_without_string_values(query: str) -> None:
    shape = query_shape(query)
    assert shape.startswith("SELECT")
    assert "private" not in shape
