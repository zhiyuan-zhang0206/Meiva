from __future__ import annotations

import psycopg
import pytest
import redis

import meiva
from base.agents.context.clients import ClientSet
from base.agents.lifecycle import SystemHalt
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.telemetry import audit_events
from meiva.tests.fixtures.pin_agent import pin_agent


# meiva:note
# A redis that is down: a real client aimed at a port nothing listens on, so the wake publish
# fails the way an unreachable cluster redis does.
def _down_redis() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)


def test_compact_survives_publish_failure(
    redis_url: str,
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(
        agent_id,
        clients=ClientSet(
            database=lambda: Database.from_settings(gate=database_gate), redis=_down_redis
        ),
    )

    with pytest.raises(SystemHalt):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind FROM inbound_messages WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "compact_summary"


def test_compact_records_its_audit_fact_with_the_summary_inbound(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)

    with pytest.raises(SystemHalt):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    rows = db_conn.execute(
        "SELECT source, attributes->>'compact_kind' FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'compact'",
        (agent_id,),
    ).fetchall()
    assert rows == [("self", "summary")]


# meiva:note
# A compact whose audit fact cannot be recorded commits no summary. The audit write is refused
# by replacing the store's record function: a real constraint violation would need DDL on the
# audit table, which blocks on the locks the live fixtures hold. Retained patch.
def test_compact_whose_audit_fact_cannot_be_recorded_commits_no_summary(
    redis_url: str,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)

    def refuse(_conn: object, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit_events, "record_audit", refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    count = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'compact_summary'",
        (agent_id,),
    ).fetchone()
    assert count == (0,)
