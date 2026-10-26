# meiva:note
# The lifecycle entries of `meiva.self` against the real throwaway Postgres and Redis: the
# statements `meiva/self/plans.py` returns are executed on the agent's own database, and the
# wake they send is received on the agent's inbound channel. The statements themselves are
# asserted as values in `test_self_plans.py`; this file only checks that the shell runs them.
# meiva:link meiva/self/docs/self.meiva.okf.md

from __future__ import annotations

from typing import Any

import psycopg
import pytest

import meiva
from base.agents.lifecycle import AgentRestart, AgentTermination
from base.agents.tests.units import spawn_agent
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.events.live.redis_client import open_sync_redis
from base.events.live.tests.rng import seeded_auth_retry
from base.lm.catalog import ModelCatalog
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva.tests.namespaces.self.redis_view import loose


def _inbound(db_conn: psycopg.Connection[Any], agent_id: int, kind: str) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT source, content, payload FROM inbound_messages "
        "WHERE agent_id = %s AND kind = %s ORDER BY id",
        (agent_id, kind),
    ).fetchall()


def _spawned(
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> int:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)
    return agent_id


def test_terminate_queues_its_inbound_row_and_wakes_the_agents_own_channel(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)
    listener = open_sync_redis(redis_url, decode_responses=True, auth_retry=seeded_auth_retry())
    subscription = loose(listener).pubsub()
    subscription.subscribe(inbound_channel(agent_id, settings.data_plane.events_channel))
    assert subscription.get_message(timeout=5)["type"] == "subscribe"

    with pytest.raises(AgentTermination):
        meiva.self.terminate()

    assert _inbound(db_conn, agent_id, "terminate") == [("self", "", None)]
    woken = subscription.get_message(timeout=5)
    subscription.close()
    listener.close()
    assert woken is not None and woken["data"] == "0"


def test_restart_without_an_overlay_queues_a_bare_restart_row(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)

    with pytest.raises(AgentRestart):
        meiva.self.restart()

    assert _inbound(db_conn, agent_id, "restart") == [("self", "", None)]
    overlay = db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert overlay is not None and not overlay[0]


@pytest.mark.usefixtures("sdk_model_owner")
def test_pause_heartbeat_logs_the_pause_and_sets_the_agents_paused_until(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)

    meiva.self.pause_heartbeat(90)

    logged = db_conn.execute(
        "SELECT duration_s FROM heartbeat_pause_log WHERE agent_id = %s", (agent_id,)
    ).fetchall()
    assert logged == [(90.0,)]
    paused = db_conn.execute(
        "SELECT heartbeat_paused_until > now() + interval '60 seconds' FROM agents_meta "
        "WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert paused == (True,)


@pytest.mark.usefixtures("sdk_model_owner")
def test_pause_heartbeat_above_the_agents_limit_writes_nothing(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)

    with pytest.raises(ValueError, match="at most"):
        meiva.self.pause_heartbeat(10**9)

    count = db_conn.execute(
        "SELECT count(*) FROM heartbeat_pause_log WHERE agent_id = %s", (agent_id,)
    ).fetchone()
    assert count == (0,)
