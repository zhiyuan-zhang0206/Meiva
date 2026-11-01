from __future__ import annotations

import os
from pathlib import Path

import psycopg

from agent.graph.exec._result import ExecLifecycleKind
from agent.graph.exec.node import ExecLifecycle
from agent.graph.exec.tests.process_inputs import real_parent
from agent.graph.exec.tests.real_child.test_exec_subprocess import AGENT_ID, run_exec_child
from base.config import settings


def _seed_agent_for_self_lifecycle() -> None:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (%s)", (AGENT_ID,))


def _self_inbound_row() -> tuple[str, str, str]:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (AGENT_ID,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    return rows[0]


def _self_lifecycle_code(action: str) -> str:
    return (
        "import meiva, os, psycopg\n"
        "from base.host.env.registry import ADMIN_DATA_PLANE_ALIASES\n"
        "assert not ADMIN_DATA_PLANE_ALIASES.intersection(os.environ)\n"
        "with psycopg.connect(meiva.DB_URL) as conn:\n"
        "    assert conn.execute('SELECT current_user').fetchone() == ('meiva_g0_runner',)\n"
        f"{action}\n"
    )


async def test_subprocess_self_terminate_lifecycle_and_inbound(
    tmp_path: Path, runner_db_url: str
) -> None:
    _seed_agent_for_self_lifecycle()

    result = await run_exec_child(
        tmp_path,
        _self_lifecycle_code("meiva.self.terminate()"),
        parent=real_parent(environ={**os.environ, "MEIVA_DB_URL": runner_db_url}),
    )

    assert isinstance(result, ExecLifecycle)
    assert result.kind is ExecLifecycleKind.TERMINATION
    _content, kind, source = _self_inbound_row()
    assert kind == "terminate"
    assert source == "self"


async def test_subprocess_self_restart_lifecycle_and_inbound(
    tmp_path: Path, runner_db_url: str
) -> None:
    _seed_agent_for_self_lifecycle()

    result = await run_exec_child(
        tmp_path,
        _self_lifecycle_code('meiva.self.restart("restarting")'),
        parent=real_parent(environ={**os.environ, "MEIVA_DB_URL": runner_db_url}),
    )

    assert isinstance(result, ExecLifecycle)
    assert result.kind is ExecLifecycleKind.RESTART
    _content, kind, source = _self_inbound_row()
    assert kind == "restart"
    assert source == "self"


async def test_subprocess_self_compact_lifecycle_and_inbound(
    tmp_path: Path, runner_db_url: str
) -> None:
    _seed_agent_for_self_lifecycle()

    result = await run_exec_child(
        tmp_path,
        _self_lifecycle_code("meiva.self.compact('audit e2e summary')"),
        parent=real_parent(environ={**os.environ, "MEIVA_DB_URL": runner_db_url}),
    )

    assert isinstance(result, ExecLifecycle)
    assert result.kind is ExecLifecycleKind.SYSTEM_HALT
    content, kind, _source = _self_inbound_row()
    assert kind == "compact_summary"
    assert content == "audit e2e summary"
