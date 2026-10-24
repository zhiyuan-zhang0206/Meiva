from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.lm.catalog import ModelCatalog
from base.paths import meiva_home
from base.sessions.pty import allocation_freeze
from base.sessions.pty.tests.pty_service import PtyServiceProcess
from services.agent_runner.page_server.page_plan import PageKey, ServerHandle
from services.agent_runner.page_server.tests.page_pty import (
    free_port,
    insert_page_row,
    page_state,
    reconcile,
    session_names,
)

pytestmark = pytest.mark.usefixtures("page_server_identity")


def _page(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    name: str,
) -> int:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    serve_dir = tmp_path / name
    serve_dir.mkdir()
    insert_page_row(db_conn, agent_id, name, free_port(), serve_dir)
    return agent_id


def test_an_absent_session_service_defers_the_page_instead_of_closing_it(
    redis_url: str,
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    unit_home: Path,
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
    )
    managed: dict[PageKey, ServerHandle] = {}

    reconcile(sync_pool, managed)

    assert page_state(db_conn, agent_id, "deferred").closed is False
    assert page_state(db_conn, agent_id, "deferred").session_name is None
    assert (agent_id, "deferred") not in managed

    # meiva:note
    # The service comes up: the next pass admits the same row.
    service = PtyServiceProcess(unit_home)
    service.start()
    try:
        reconcile(sync_pool, managed)

        state = page_state(db_conn, agent_id, "deferred")
        assert state.closed is False
        assert state.session_name is not None
        assert managed[(agent_id, "deferred")].session_name == state.session_name
        assert session_names() == {state.session_name}
    finally:
        service.stop()


def test_one_pass_attempts_each_deferred_page_once(
    redis_url: str,
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agents = [
        _page(
            db_conn,
            tmp_path,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            machine_identity=machine_identity,
            name=name,
        )
        for name in ("a", "b", "c")
    ]

    reconcile(sync_pool, {})

    # meiva:note
    # A deferred row waits for the next pass; deferring must not become a retry loop
    # inside one pass (the bound that keeps a pass proportional to its rows). Every attempt
    # takes a session index from the owning agent's counter, so one attempt per row leaves
    # each counter at one.
    counters = [
        db_conn.execute(
            "SELECT session_index FROM agents_meta WHERE id = %s", (agent_id,)
        ).fetchone()
        for agent_id in agents
    ]
    assert counters == [(1,), (1,), (1,)]


@pytest.mark.usefixtures("pty_service")
def test_a_refused_creation_still_closes_the_page(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    redis_url: str,
) -> None:
    agent_id = _page(
        db_conn,
        tmp_path,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        machine_identity=machine_identity,
        name="refused",
    )
    frozen = allocation_freeze.freeze(
        holder="operator",
        reason="page admission test",
        writer=AtomicWriter(SYSTEM_FS),
        home=meiva_home(),
    )

    try:
        reconcile(sync_pool, {})
    finally:
        assert frozen.generation is not None
        allocation_freeze.resume(
            frozen.generation, writer=AtomicWriter(SYSTEM_FS), home=meiva_home()
        )

    # meiva:note
    # The service answered and still refused: that is this page's failure, and the
    # claim is released so the closed row carries no reserved name.
    state = page_state(db_conn, agent_id, "refused")
    assert (state.closed, state.session_name) == (True, None)
    assert session_names() == set()
