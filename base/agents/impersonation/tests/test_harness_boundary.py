from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import sessions
from base.api_contracts.agent_contract import Harness
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.events.live.bus import EventBus


def create_target(conn: psycopg.Connection, harness: Harness) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,harness) VALUES(%s,'idling',%s,%s)",
        (agent_id, "harness-test", harness.value),
    )
    conn.commit()
    return agent_id


def request_takeover(
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    agent_id: int,
) -> dict[str, Any]:
    return sessions.request(
        database,
        event_bus,
        agent_id,
        authority=config_authority,
        name="Harness boundary",
        executor_name="Codex reviewer",
        provider="codex",
        process_metadata={},
        thread_id=str(uuid4()),
        local_machine="harness-test",
    )


@pytest.mark.parametrize("harness", [value for value in Harness if value is not Harness.MEIVA])
def test_external_harness_refuses_takeover_without_creating_a_lease(
    harness: Harness,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = create_target(db_conn, harness)

    with pytest.raises(leases.ImpersonationError, match=f"Meiva harness.*{harness.value}"):
        request_takeover(database, event_bus, config_authority, agent_id)

    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonations WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT status,harness FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling", harness.value)


def test_meiva_harness_still_accepts_a_codex_controller(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = create_target(db_conn, Harness.MEIVA)

    result = request_takeover(database, event_bus, config_authority, agent_id)

    assert result["status"] == "preparing"
    assert db_conn.execute(
        "SELECT automatic,relay_provider FROM agent_impersonations WHERE agent_id=%s",
        (agent_id,),
    ).fetchone() == (True, "codex")
