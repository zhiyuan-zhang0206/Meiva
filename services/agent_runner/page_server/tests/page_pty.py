# meiva:note
# Real resources for the page-server pass: the pty-sessions service of the test home, page-server
# processes, and the rows they serve.
#
# `reconcile` runs the production pass against the throwaway Postgres and whatever pty service the
# test started (none is a real outage). A page the pass creates is a real login shell running the
# real page server, so a test asserts what the pass did through the service's session list and the
# server's own health answer.

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from base.events.live.bus import EventBus
from base.host.env import port_lease
from base.sessions.backend import PtySessionBackend
from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import service_socket
from base.sessions.pty.tests.pty_shells import wait_for
from services.agent_runner.page_server import daemon
from services.agent_runner.page_server.page_plan import (
    PageKey,
    PageRow,
    ServerHandle,
)

__all__ = [
    "HOST",
    "PageState",
    "close_row",
    "free_port",
    "insert_page_row",
    "leaving_open_set_after_claim",
    "page_state",
    "reconcile",
    "reconcile_snapshot",
    "refusing_show_page_close",
    "running_page_server",
    "session_names",
    "wait_until_healthy",
]

HOST = "127.0.0.1"
_REPO = Path(__file__).resolve().parents[4]


# meiva:note
# A port nothing listens on right now.
# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def free_port() -> int:
    return port_lease.lease_ports(1)[0]


# meiva:note
# The backend the daemon uses, with the client's connect patience set to a value a test can wait
# out: a service that is down answers at once instead of after the production five seconds.
def _backend() -> PtySessionBackend:
    return PtySessionBackend(
        client.PtyClientConfig(socket_path=service_socket(), connect_wait_s=0.0)
    )


def insert_page_row(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    serve_dir: Path,
    *,
    token: str | None = None,
    session: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir, server_token, session_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (agent_id, name, port, HOST, str(serve_dir), token, session),
        )
    conn.commit()


def close_row(conn: psycopg.Connection, agent_id: int, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
    conn.commit()


@dataclass(frozen=True)
class PageState:
    closed: bool
    session_name: str | None
    token: str | None


def page_state(conn: psycopg.Connection, agent_id: int, name: str) -> PageState:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT closed_at IS NOT NULL OR expired_at IS NOT NULL, session_name, server_token "
            "FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        row = cur.fetchone()
    assert row is not None
    return PageState(bool(row[0]), row[1], row[2])


# meiva:note
# One production pass against the pty service of the test home.
def reconcile(pool: ConnectionPool, managed: dict[PageKey, ServerHandle]) -> None:
    daemon.reconcile_once(pool, managed, HOST, EventBus.from_settings(), _backend())


# meiva:note
# One production pass over rows the test read earlier, as a pass whose snapshot went stale.
def reconcile_snapshot(
    pool: ConnectionPool, managed: dict[PageKey, ServerHandle], rows: list[PageRow]
) -> None:
    daemon.reconcile_rows(pool, managed, rows, HOST, EventBus.from_settings(), _backend())


def session_names() -> set[str]:
    return set(client.live_sessions(socket_path=service_socket()))


def wait_until_healthy(port: int, token: str, *, timeout: float = 30.0) -> None:
    assert wait_for(lambda: daemon.server_is_healthy(HOST, port, token), timeout), (
        f"no page server answered ok:{token} on port {port}"
    )


# meiva:note
# A page server running outside any page shell, as a detached or foreign one would be.
@contextmanager
def running_page_server(
    serve_dir: Path, port: int, token: str, *, home: Path | None = None
) -> Generator[subprocess.Popen[bytes]]:
    env = {**os.environ, "PAGE_SERVER_TOKEN": token}
    if home is not None:
        env["MEIVA_HOME"] = str(home)
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "services.agent_runner.page_server.server",
            "--port",
            str(port),
            "--host",
            HOST,
            "--dir",
            str(serve_dir),
        ],
        cwd=_REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_healthy(port, token)
        yield process
    finally:
        process.kill()
        process.wait(timeout=10)


# meiva:note
# The row leaves the open set right after a pass claims its session name, produced by the
# throwaway Postgres itself.
#
# An AFTER UPDATE trigger stamps `column` on the row in the claim's own transaction, so the claim
# commits with the row already closed (or expired) and the pass finds that out only at its next
# write, after the shell exists: the narrow window a page can close in while its session is created.
@contextmanager
def leaving_open_set_after_claim(
    conn: psycopg.Connection, column: Literal["closed_at", "expired_at"]
) -> Generator[None]:
    function = sql.Identifier("page_row_leaves_after_claim")
    conn.commit()
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN "
            "UPDATE agent_pages SET {column} = now() WHERE id = NEW.id; RETURN NULL; END $body$"
        ).format(function=function, column=sql.Identifier(column))
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {function} AFTER UPDATE OF session_name ON agent_pages "
            "FOR EACH ROW WHEN (OLD.session_name IS NULL AND NEW.session_name IS NOT NULL) "
            "EXECUTE FUNCTION {function}()"
        ).format(function=function)
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP TRIGGER {function} ON agent_pages").format(function=function))
        conn.execute(sql.SQL("DROP FUNCTION {function}()").format(function=function))
        conn.commit()


# meiva:note
# Closing a show page fails in the database, produced by the throwaway Postgres itself.
#
# A BEFORE UPDATE trigger raises for the rows that have no serve directory, the only rows the
# dead-show-page scan closes, so a dead show page makes its round fail the way a database error
# during the close would, and a serve page is left untouched.
@contextmanager
def refusing_show_page_close(conn: psycopg.Connection) -> Generator[None]:
    function = sql.Identifier("page_show_close_refused")
    conn.commit()
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN "
            "IF NEW.serve_dir IS NULL AND NEW.closed_at IS NOT NULL THEN "
            "RAISE EXCEPTION 'show page close refused by the test'; END IF; "
            "RETURN NEW; END $body$"
        ).format(function=function)
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {function} BEFORE UPDATE ON agent_pages "
            "FOR EACH ROW EXECUTE FUNCTION {function}()"
        ).format(function=function)
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP TRIGGER {function} ON agent_pages").format(function=function))
        conn.execute(sql.SQL("DROP FUNCTION {function}()").format(function=function))
        conn.commit()
