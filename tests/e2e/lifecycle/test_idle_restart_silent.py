from __future__ import annotations

import time
from typing import Any

import httpx
import psycopg
import pytest
from langchain_core.messages import AIMessage

from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver as PostgresSaver
from base.agents.history.delta_read_compat import reconstruct_delta_messages
from base.api_contracts.agent_contract import AgentStatus
from base.config import settings
from tests.e2e._ports import GATEWAY_URL


def _checkpoint_values(agent_id: int) -> dict[str, Any]:
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": str(agent_id)}})
        if tup is not None:
            reconstruct_delta_messages(saver, tup)
    if tup is None:
        return {}
    return tup.checkpoint.get("channel_values", {})


def _ai_count(values: dict[str, Any]) -> int:
    return sum(1 for m in values.get("messages", []) if isinstance(m, AIMessage))


def _agent_status(agent_id: int) -> str | None:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    return None if row is None else row[0]


def _restart_inbound_applied(agent_id: int) -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status = 'done' FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart' LIMIT 1",
            (agent_id,),
        )
        return cur.fetchone() == (True,)


def _wait_for_pre_restart_turn(agent_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        values = _checkpoint_values(agent_id)
        if _agent_status(agent_id) == AgentStatus.IDLING.value and _ai_count(values) == 2:
            return values
        time.sleep(0.3)
    raise RuntimeError(f"agent {agent_id} did not complete pre-restart turn within 90s")


def _wait_for_silent_restart_cycle(agent_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 90.0
    last: tuple[bool, str | None, bool] = (False, None, False)
    while time.monotonic() < deadline:
        completed = _restart_inbound_applied(agent_id)
        status = _agent_status(agent_id)
        values = _checkpoint_values(agent_id)
        marker_committed = any(
            "Restart was accepted from user" in str(m.content) for m in values.get("messages", [])
        )
        last = (completed, status, marker_committed)
        if completed and status == AgentStatus.IDLING.value and marker_committed:
            return values
        time.sleep(0.5)
    raise RuntimeError(
        f"agent {agent_id} did not complete silent restart cycle within 90s: "
        f"(completed, status, marker_committed)={last!r}"
    )


def _assert_silent_and_alive_after_restart(values: dict[str, Any]) -> None:
    assert _ai_count(values) == 2, f"extra LLM turn after respawn: {_ai_count(values)} AIMessages"
    assert values["halted"] is True
    assert str(values["meiva_code__cwd"]).endswith("e2e-idle-restart-cwd"), (
        f"plugin state cwd did not survive respawn: {values.get('meiva_code__cwd')!r}"
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.idle_restart_silent:build")
def test_external_restart_of_idle_agent_is_silent(spawned_agent: int) -> None:
    agent_id = spawned_agent

    resp = httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/messages",
        json={"content": "\u8bf7\u8bbe\u7f6e\u5de5\u4f5c\u76ee\u5f55", "source": "user"},
        timeout=10.0,
    )
    resp.raise_for_status()

    values = _wait_for_pre_restart_turn(agent_id)

    assert str(values["meiva_code__cwd"]).endswith("e2e-idle-restart-cwd"), (
        f"pre-restart cwd not written to plugin state: {values.get('meiva_code__cwd')!r}"
    )

    httpx.post(f"{GATEWAY_URL}/api/agents/{agent_id}/restart", timeout=10.0).raise_for_status()

    values = _wait_for_silent_restart_cycle(agent_id)

    _assert_silent_and_alive_after_restart(values)

    time.sleep(2.0)
    values = _checkpoint_values(agent_id)
    assert _agent_status(agent_id) == AgentStatus.IDLING.value
    assert _ai_count(values) == 2
