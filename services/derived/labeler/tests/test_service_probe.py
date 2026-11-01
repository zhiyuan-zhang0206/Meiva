# meiva:note
# The labeler's run test asserts its pool through `wait_connected`; these cases pin the waiting
# itself, so the helper cannot regress into a single read that a still-connecting pool passes.
from __future__ import annotations

import asyncio
import time

import psycopg
import pytest

from services.derived.labeler.tests.service_probe import our_backends, wait_connected


async def test_wait_connected_waits_for_a_connection_that_arrives_later(
    db_url: str, db_conn: psycopg.Connection
) -> None:
    name = "meiva:labeler-wait-probe"
    assert our_backends(db_conn, application_name=name) == 0

    def late_connection() -> psycopg.Connection:
        time.sleep(0.3)
        return psycopg.connect(db_url, application_name=name)

    late = asyncio.get_running_loop().run_in_executor(None, late_connection)
    try:
        await wait_connected(db_conn, application_name=name)
        assert our_backends(db_conn, application_name=name) == 1
    finally:
        (await late).close()


async def test_wait_connected_fails_when_no_connection_arrives(
    db_conn: psycopg.Connection,
) -> None:
    with pytest.raises(pytest.fail.Exception, match="never opened a database connection"):
        await wait_connected(db_conn, application_name="meiva:labeler-never", timeout_s=0.2)
