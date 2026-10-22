from __future__ import annotations

from typing import Any

import psycopg
import pytest

from base import cluster
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from gateway.events import telemetry_rows


@pytest.fixture
def fake_admin_events(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Patch telemetry_rows.query_events for the admin-events route; record the
    kwargs and return canned rows."""

    calls: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    def _query(_conn: object, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        calls.append(kwargs)
        return rows, False

    monkeypatch.setattr(telemetry_rows, "query_events", _query)
    return {"calls": calls, "rows": rows}


@pytest.fixture(autouse=True)
def pinned_session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the session-name composer for gateway cluster tests."""
    # meiva:note
    # The test stub uses an untyped lambda over the service name.
    monkeypatch.setattr(cluster, "session_name", lambda svc: f"meiva-test-{svc}")  # pyright: ignore[reportUnknownArgumentType]


def seed_away_machine(
    db_conn: psycopg.Connection, *, name: str = "away", local_row: bool = True
) -> None:
    # meiva:note
    # Machines rows for the pause tests, isolated from other tests' rows.
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        if local_row:
            cur.execute(
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], NULL)"
            )
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) "
            "VALUES (%s, ARRAY['agent-runner'], NULL)",
            (name,),
        )
    db_conn.commit()


def seed_agent_on_machine(
    db_conn: psycopg.Connection,
    machine: str,
    *,
    status: str = "idling",
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    # meiva:note
    # One live agent row homed on `machine`.
    from base.agents.tests.units import spawn_agent

    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = %s WHERE id = %s",
            (machine, status, aid),
        )
    db_conn.commit()
    return aid


# meiva:note
# The pause endpoint reassigns a paused machine's tasks to a fixed drain owner
# (`_MACHINE_PAUSE_DRAIN_OWNER`, #405), so the row has to exist here as the FK
# target. Writing an id by hand does not move the id sequence, and the next spawn
# then takes a number that is already in the table: with two spawns, the second
# took 405 and the insert failed on `agents_pkey`. Moving the sequence past every
# id the table holds makes the manual row a reservation instead of a trap.
# meiva:link gateway/tests/routers/cluster_endpoints/test_admin_machine_delete.py
def seed_drain_owner(db_conn: psycopg.Connection, agent_id: int = 405) -> None:
    # meiva:note
    # The drain-owner agent row the pause endpoint reassigns tasks to.
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (agent_id,),
        )
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('agents', 'id'), (SELECT max(id) FROM agents))"
        )
    db_conn.commit()


def seed_in_progress_task(db_conn: psycopg.Connection, owner: int, title: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by) "
            "VALUES (%s, 'desc', 'in_progress', %s, 'user') RETURNING id",
            (title, owner),
        )
        row = cur.fetchone()
        assert row is not None
        (task_id,) = row
    db_conn.commit()
    return task_id
