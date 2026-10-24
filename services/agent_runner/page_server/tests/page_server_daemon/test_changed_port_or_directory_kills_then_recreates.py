# meiva:note
# Page server daemon cases: changed port or directory kills then recreates, and reclaim.

from __future__ import annotations

import secrets
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.agent_runner.page_server.daemon as psd
from services.agent_runner.page_server.page_plan import PageKey, ServerHandle
from services.agent_runner.page_server.tests.page_pty import (
    HOST,
    free_port,
    insert_page_row,
    page_state,
    reconcile,
    running_page_server,
    session_names,
    wait_until_healthy,
)

pytestmark = pytest.mark.usefixtures("page_server_identity", "pty_service")


def test_changed_port_or_directory_kills_then_recreates_the_page_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    tmp_path: Path,
) -> None:
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    insert_page_row(db_conn, agent_id, "changed", free_port(), old_dir)
    managed: dict[PageKey, ServerHandle] = {}
    reconcile(sync_pool, managed)
    old_session = managed[(agent_id, "changed")].session_name
    new_port = free_port()
    db_conn.execute(
        "UPDATE agent_pages SET port = %s, serve_dir = %s, session_name = NULL, "
        "server_token = NULL WHERE agent_id = %s AND name = %s",
        (new_port, str(new_dir), agent_id, "changed"),
    )
    db_conn.commit()

    reconcile(sync_pool, managed)

    new_session = managed[(agent_id, "changed")].session_name
    assert new_session != old_session
    assert session_names() == {new_session}
    token = page_state(db_conn, agent_id, "changed").token
    assert token is not None
    wait_until_healthy(new_port, token)


def test_reclaim_preserves_in_session_server_and_kills_detached_orphan(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
    unit_home: Path,
    tmp_path: Path,
) -> None:
    key = (agent_id, "kept")
    kept_port = free_port()
    insert_page_row(db_conn, agent_id, key[1], kept_port, serve_dir)
    managed: dict[PageKey, ServerHandle] = {}
    reconcile(sync_pool, managed)
    kept_token = page_state(db_conn, agent_id, key[1]).token
    assert kept_token is not None
    wait_until_healthy(kept_port, kept_token)
    foreign_home = tmp_path / "another-cluster"
    foreign_home.mkdir()

    with (
        running_page_server(serve_dir, free_port(), "orphan-token") as orphan,
        running_page_server(serve_dir, free_port(), "foreign-token", home=foreign_home) as foreign,
    ):
        reconcile(sync_pool, managed)

        assert orphan.wait(timeout=10) is not None, "the detached orphan was not killed"
        assert foreign.poll() is None, "a server of another cluster home was killed"
    assert psd.server_is_healthy(HOST, kept_port, kept_token)
    assert session_names() == {managed[key].session_name}


# meiva:note
# A server nobody supervises on a port a row wants is killed so the row's own shell can bind.
def test_a_detached_server_on_a_wanted_port_is_reclaimed(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    agent_id: int,
    serve_dir: Path,
) -> None:
    port = free_port()
    insert_page_row(
        db_conn,
        agent_id,
        "wanted",
        port,
        serve_dir,
        token=secrets.token_hex(16),
        session=f"meiva-agent-{agent_id}-shell-0-page-wanted",
    )

    with running_page_server(serve_dir, port, "stray-token") as stray:
        reconcile(sync_pool, {})

        assert stray.wait(timeout=10) is not None, "the stray server kept the wanted port"


def test_server_module_still_serves_a_tokenized_health_endpoint(tmp_path: Path) -> None:
    port = free_port()
    with running_page_server(tmp_path, port, "roundtrip"):
        assert psd.server_is_healthy(HOST, port, "roundtrip")
        assert not psd.server_is_healthy(HOST, port, "another-token")
