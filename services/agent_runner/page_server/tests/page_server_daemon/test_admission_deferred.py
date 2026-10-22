from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

import services.agent_runner.page_server.daemon as psd
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.agent_runner.page_server.tests.page_shell import PageShellBackend
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    backend as backend,
)
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    insert_page_row,
    reconcile,
)
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    page_server_identity as page_server_identity,
)
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    sync_pool as sync_pool,
)


def _row(conn: psycopg.Connection, agent_id: int, name: str) -> tuple[bool, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT closed_at IS NOT NULL, session_name FROM agent_pages "
            "WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        fetched = cur.fetchone()
    assert fetched is not None
    closed, session = fetched
    return bool(closed), None if session is None else str(session)


def _page(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    name: str,
    port: int,
) -> int:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    serve_dir = tmp_path / name
    serve_dir.mkdir()
    insert_page_row(db_conn, agent_id, name, port, serve_dir)
    return agent_id


def test_an_absent_session_service_defers_the_page_instead_of_closing_it(
    redis_url: str,
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: PageShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = _page(
        db_conn,
        tmp_path,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        machine_identity=machine_identity,
        name="deferred",
        port=12107,
    )
    backend.new_result = False
    backend.service_available_result = False
    managed: dict[tuple[int, str], psd.ServerHandle] = {}

    reconcile(sync_pool, managed)

    assert len(backend.new_calls) == 1
    assert _row(db_conn, agent_id, "deferred") == (False, None)
    assert (agent_id, "deferred") not in managed

    # meiva:note
    # The service comes back: the next pass admits the same row, with the name the
    # previous pass had already reserved and rolled back.
    backend.new_result = True
    backend.service_available_result = True
    reconcile(sync_pool, managed)

    closed, session = _row(db_conn, agent_id, "deferred")
    assert closed is False
    assert session is not None
    assert managed[(agent_id, "deferred")].session_name == session


def test_one_pass_attempts_each_deferred_page_once(
    redis_url: str,
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: PageShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    for index, name in enumerate(("a", "b", "c")):
        _page(
            db_conn,
            tmp_path,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            machine_identity=machine_identity,
            name=name,
            port=12110 + index,
        )
    backend.new_result = False
    backend.service_available_result = False

    reconcile(sync_pool, {})

    # meiva:note
    # A deferred row waits for the next pass; deferring must not become a retry loop
    # inside one pass (the bound that keeps a pass proportional to its rows).
    assert len(backend.new_calls) == 3


def test_a_refused_creation_still_closes_the_page(
    redis_url: str,
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: PageShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = _page(
        db_conn,
        tmp_path,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        machine_identity=machine_identity,
        name="refused",
        port=12120,
    )
    backend.new_result = False
    backend.service_available_result = True

    reconcile(sync_pool, {})

    # meiva:note
    # The service answered and still refused: that is this page's failure, and the
    # claim is released so the closed row carries no reserved name.
    assert _row(db_conn, agent_id, "refused") == (True, None)
