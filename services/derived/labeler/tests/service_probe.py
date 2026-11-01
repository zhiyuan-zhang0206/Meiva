# meiva:note
# Observing a running service from outside: its health port accepts connections while it serves,
# and when it has stopped its pidfile, port and database connections are gone.
from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import psycopg
import pytest


def _accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.2):
            return True
    except OSError:
        return False


async def wait_serving(port: int, pidfile: Path, *, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pidfile.exists() and _accepts(port):
            return
        await asyncio.sleep(0.02)
    pytest.fail("the service never started serving its health endpoint")


def our_backends(db_conn: psycopg.Connection, *, application_name: str) -> int:
    db_conn.rollback()
    row = db_conn.execute(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND application_name = %s",
        (application_name,),
    ).fetchone()
    assert row is not None
    return int(row[0])


# meiva:note
# `daemon.run` publishes the pidfile, binds healthz, and only then builds the pool, so the health
# port accepting connections says nothing yet about the daemon's database connection. A test that
# reads the connection count right after `wait_serving` is racing the pool's first connection.
async def wait_connected(
    db_conn: psycopg.Connection, *, application_name: str, timeout_s: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if our_backends(db_conn, application_name=application_name):
            return
        await asyncio.sleep(0.02)
    pytest.fail("the service never opened a database connection")


async def assert_released(
    db_conn: psycopg.Connection,
    pidfile: Path,
    port: int,
    *,
    application_name: str,
    timeout_s: float = 15.0,
) -> None:
    assert not pidfile.exists()
    with pytest.raises(ConnectionRefusedError), socket.create_connection(("127.0.0.1", port), 1.0):
        pass
    deadline = time.monotonic() + timeout_s
    while our_backends(db_conn, application_name=application_name) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    remaining = our_backends(db_conn, application_name=application_name)
    if remaining:
        rows = db_conn.execute(
            "SELECT pid, application_name, backend_start, state, client_addr::text "
            "FROM pg_stat_activity "
            "WHERE datname = current_database() AND application_name = %s ORDER BY pid LIMIT 20",
            (application_name,),
        ).fetchall()
        pytest.fail(
            f"the service left its pool open: {remaining} owned backends; first 20: {rows!r}"
        )
