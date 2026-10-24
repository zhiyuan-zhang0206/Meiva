# meiva:note
# Observing a running service from outside: its health endpoint answers, and when it has stopped its
# pidfile, port and database connections are gone.
from __future__ import annotations

import asyncio
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

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


# meiva:note
# The status and JSON body of the service's `/healthz`; a 503 is an answer, not an error.
def healthz(port: int) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5.0) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def components(body: dict[str, Any]) -> list[dict[str, Any]]:
    value = body["components"]
    assert isinstance(value, list)
    return cast("list[dict[str, Any]]", value)


def component_names(body: dict[str, Any]) -> list[str]:
    return [str(item["name"]) for item in components(body)]


def our_backends(db_conn: psycopg.Connection) -> int:
    db_conn.rollback()
    row = db_conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'meiva:%%'"
    ).fetchone()
    assert row is not None
    return int(row[0])


async def assert_released(
    db_conn: psycopg.Connection, pidfile: Path, port: int, *, timeout_s: float = 15.0
) -> None:
    assert not pidfile.exists()
    with pytest.raises(ConnectionRefusedError), socket.create_connection(("127.0.0.1", port), 1.0):
        pass
    deadline = time.monotonic() + timeout_s
    while our_backends(db_conn) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert our_backends(db_conn) == 0, "the service left its pool open"
