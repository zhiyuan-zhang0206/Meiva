# meiva:note
# Page-server daemon pass against the real pty service, real shells and the throwaway Postgres.

from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path
from typing import Literal

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.agent_runner.page_server.daemon as psd
from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import service_socket
from services.agent_runner.page_server.page_plan import PageKey, ServerHandle
from services.agent_runner.page_server.tests.page_pty import (
    HOST,
    close_row,
    free_port,
    insert_page_row,
    leaving_open_set_after_claim,
    page_state,
    reconcile,
    reconcile_snapshot,
    session_names,
    wait_until_healthy,
)

pytestmark = pytest.mark.usefixtures("page_server_identity", "pty_service")


def test_open_row_creates_a_persistent_page_session_that_serves_with_its_token(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    port = free_port()
    insert_page_row(db_conn, agent_id, "My_Page", port, serve_dir)
    managed: dict[PageKey, ServerHandle] = {}

    reconcile(sync_pool, managed)

    state = page_state(db_conn, agent_id, "My_Page")
    assert state.session_name == f"meiva-agent-{agent_id}-shell-0-page-my-page"
    assert state.session_name is not None and state.token is not None
    assert set(managed) == {(agent_id, "My_Page")}
    assert session_names() == {state.session_name}
    assert client.live_sessions(socket_path=service_socket())[state.session_name].cwd == str(
        serve_dir
    )
    wait_until_healthy(port, state.token)

    reconcile(sync_pool, managed)
    assert page_state(db_conn, agent_id, "My_Page") == state
    assert session_names() == {state.session_name}


def test_a_restarted_daemon_adopts_the_live_session_of_a_healthy_page(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    port = free_port()
    insert_page_row(db_conn, agent_id, "adopted", port, serve_dir)
    reconcile(sync_pool, {})
    state = page_state(db_conn, agent_id, "adopted")
    assert state.token is not None
    wait_until_healthy(port, state.token)
    (before,) = client.list_sessions(socket_path=service_socket())

    restarted: dict[PageKey, ServerHandle] = {}
    reconcile(sync_pool, restarted)

    assert set(restarted) == {(agent_id, "adopted")}
    assert restarted[(agent_id, "adopted")].session_name == state.session_name
    assert [info.pid for info in client.list_sessions(socket_path=service_socket())] == [before.pid]
    wait_until_healthy(port, state.token)


def test_closed_row_kills_its_page_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    insert_page_row(db_conn, agent_id, "closed", free_port(), serve_dir)
    managed: dict[PageKey, ServerHandle] = {}
    reconcile(sync_pool, managed)
    page_session = managed[(agent_id, "closed")].session_name
    assert session_names() == {page_session}

    close_row(db_conn, agent_id, "closed")
    reconcile(sync_pool, managed)

    assert session_names() == set()
    assert managed == {}


def test_stale_snapshot_never_creates_session_for_closed_row(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    """The pass snapshot can be seconds stale (process scans, session spawns
    happen after the row read). A row closed after the snapshot must not get a
    session created for it — the next pass would reap the fresh session as an
    orphan, the user-visible serve 502 race (rows 1643/1667 incidents)."""
    insert_page_row(db_conn, agent_id, "stale-race", free_port(), serve_dir)
    stale = psd.open_rows(sync_pool, HOST)
    close_row(db_conn, agent_id, "stale-race")
    managed: dict[PageKey, ServerHandle] = {}

    reconcile_snapshot(sync_pool, managed, stale)

    assert session_names() == set()
    assert managed == {}
    state = page_state(db_conn, agent_id, "stale-race")
    # No token is minted for a closed row (the mint is deferred until
    # after the liveness re-check, never evaluated eagerly).
    assert (state.session_name, state.token) == (None, None)


def test_stale_snapshot_skips_row_re_registered_mid_pass(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    """A row re-registered (port/serve_dir changed) after the snapshot must
    not be created from the obsolete state either — the new registration is
    adopted on the next pass instead."""
    insert_page_row(db_conn, agent_id, "re-registered", free_port(), serve_dir)
    stale = psd.open_rows(sync_pool, HOST)
    db_conn.execute(
        "UPDATE agent_pages SET port = %s WHERE agent_id = %s AND name = 're-registered'",
        (free_port(), agent_id),
    )
    db_conn.commit()
    managed: dict[PageKey, ServerHandle] = {}

    reconcile_snapshot(sync_pool, managed, stale)

    assert session_names() == set()
    assert managed == {}
    assert page_state(db_conn, agent_id, "re-registered").session_name is None


# meiva:note
# The row can still close or expire in the narrow window between the claim and the
# session_name write that follows the shell's creation. The just-created session is rolled
# back (killed) instead of lingering as a recordless ghost the next pass would reap as an
# orphan.
@pytest.mark.parametrize("column", ["expired_at", "closed_at"])
def test_a_session_created_for_a_row_that_left_the_open_set_is_rolled_back(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
    column: Literal["expired_at", "closed_at"],
) -> None:
    insert_page_row(db_conn, agent_id, "mid-create", free_port(), serve_dir)
    managed: dict[PageKey, ServerHandle] = {}

    with leaving_open_set_after_claim(db_conn, column):
        reconcile(sync_pool, managed)

    assert session_names() == set()
    assert managed == {}
    assert page_state(db_conn, agent_id, "mid-create").closed


def test_terminated_agent_keeps_daemon_page_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    """Agent termination does not close a daemon-supervised page row or shell."""
    key = (agent_id, "persistent")
    insert_page_row(db_conn, agent_id, key[1], free_port(), serve_dir)
    managed: dict[PageKey, ServerHandle] = {}
    reconcile(sync_pool, managed)
    page_session = managed[key].session_name
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
    db_conn.commit()

    reconcile(sync_pool, managed)

    assert session_names() == {page_session}
    assert set(managed) == {key}
    assert [(row.agent_id, row.name) for row in psd.open_rows(sync_pool, HOST)] == [key]


def test_daemon_restart_kills_the_persisted_session_of_a_closed_row(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    insert_page_row(db_conn, agent_id, "closed-after-restart", free_port(), serve_dir)
    reconcile(sync_pool, {})
    assert len(session_names()) == 1
    close_row(db_conn, agent_id, "closed-after-restart")

    reconcile(sync_pool, {})

    assert session_names() == set()


def test_lost_page_session_is_closed_without_relaunch_or_agent_notice(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    insert_page_row(
        db_conn,
        agent_id,
        "lost",
        free_port(),
        serve_dir,
        token=secrets.token_hex(16),
        session=f"meiva-agent-{agent_id}-shell-1-page-lost",
    )
    before = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,)
    ).fetchone()

    for _ in range(3):
        reconcile(sync_pool, {})  # New in-memory state, as after a daemon restart.

    assert session_names() == set()
    assert page_state(db_conn, agent_id, "lost").closed
    assert (
        db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,)
        ).fetchone()
        == before
    )


# meiva:note
# The handle as it is once its spawn window is over.
def _aged(handle: ServerHandle, *, launch: str | None = None) -> ServerHandle:
    return replace(
        handle,
        token=handle.token if launch is None else launch,
        last_launch_monotonic=handle.last_launch_monotonic - 60.0,
    )


def test_an_aged_handle_whose_server_answers_with_its_token_is_kept(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    key = (agent_id, "answering")
    port = free_port()
    insert_page_row(db_conn, agent_id, key[1], port, serve_dir)
    managed: dict[PageKey, ServerHandle] = {}
    reconcile(sync_pool, managed)
    wait_until_healthy(port, managed[key].token)
    managed[key] = _aged(managed[key])

    reconcile(sync_pool, managed)

    assert not page_state(db_conn, agent_id, key[1]).closed
    assert session_names() == {managed[key].session_name}


# meiva:note
# The token is what proves the server on the port is the one this launch started: a
# server that answers with another one is unavailable once the spawn window is over. The page
# closes, its shell is killed and nothing relaunches it.
def test_a_server_that_does_not_answer_with_the_launch_token_is_unavailable(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    key = (agent_id, "silent")
    port = free_port()
    insert_page_row(db_conn, agent_id, key[1], port, serve_dir)
    managed: dict[PageKey, ServerHandle] = {}
    reconcile(sync_pool, managed)
    wait_until_healthy(port, managed[key].token)
    managed[key] = _aged(managed[key], launch="another-launch")

    reconcile(sync_pool, managed)

    assert page_state(db_conn, agent_id, key[1]).closed
    assert session_names() == set()
    assert managed == {}
