from __future__ import annotations

from contextlib import AbstractContextManager

import psycopg
import pytest
import redis
from psycopg import sql

import meiva
from base.agents.context.clients import ClientSet
from base.agents.lifecycle import SystemHalt
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.agents.tests.units import spawn_agent
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from meiva.sdk_surface.installation import Installation
from meiva.sdk_surface.tests.installation_holder import held_installation
from meiva.tests.fixtures.pin_agent import pin_agent


# meiva:note
# The SDK root the compact's wake publish draws its retry jitter from: a bare installation over a
# fixed sampling owner, which carries the process generator.
def _sdk_installed() -> AbstractContextManager[Installation]:
    return held_installation(Installation.bare(sampling=fixed_sampling_owner()))


# meiva:note
# A redis that is down: a real client aimed at a port nothing listens on, so the wake publish
# fails the way an unreachable cluster redis does.
def _down_redis() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)


def test_compact_survives_publish_failure(
    redis_url: str,
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    loguru_records: LogRecords,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
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

    with _sdk_installed(), pytest.raises(SystemHalt):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind FROM inbound_messages WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "compact_summary"
    channel = inbound_channel(agent_id, settings.data_plane.events_channel)
    [attempt] = [m for m in loguru_records.messages("WARNING") if "self inbound wake" in m]
    assert repr(channel) in attempt


def test_compact_records_its_audit_fact_with_the_summary_inbound(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)

    with _sdk_installed(), pytest.raises(SystemHalt):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    rows = db_conn.execute(
        "SELECT source, attributes->>'compact_kind' FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'compact'",
        (agent_id,),
    ).fetchall()
    assert rows == [("self", "summary")]


# meiva:note
# A compact whose audit fact cannot be recorded commits no summary. The fault is a real trigger on
# the audit table that rejects the `compact` row of this test's agent, so the audit write the
# compact makes inside its transaction fails the way a violated constraint does; the trigger and its
# function are dropped afterwards.
def test_compact_whose_audit_fact_cannot_be_recorded_commits_no_summary(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)
    db_conn.execute(
        "CREATE FUNCTION reject_compact_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'audit write failed' USING ERRCODE = '23514'; END $$"
    )
    db_conn.execute(
        sql.SQL(
            "CREATE TRIGGER reject_compact_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW WHEN (NEW.event_name = 'compact' AND NEW.agent_id = {}) "
            "EXECUTE FUNCTION reject_compact_audit()"
        ).format(sql.Literal(agent_id))
    )
    db_conn.commit()
    try:
        with (
            _sdk_installed(),
            pytest.raises(psycopg.errors.CheckViolation, match="audit write failed"),
        ):
            meiva.self.compact("Requests: (none)\nProgress: done\n")
    finally:
        db_conn.rollback()
        db_conn.execute("DROP TRIGGER reject_compact_audit ON audit_events")
        db_conn.execute("DROP FUNCTION reject_compact_audit()")
        db_conn.commit()

    count = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'compact_summary'",
        (agent_id,),
    ).fetchone()
    assert count == (0,)


def test_compact_without_an_sdk_random_generator_fails_before_it_commits_a_summary(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)

    with pytest.raises(RuntimeError, match="no random generator"):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    count = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'compact_summary'",
        (agent_id,),
    ).fetchone()
    assert count == (0,)
