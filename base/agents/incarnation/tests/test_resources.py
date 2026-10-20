from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.agents.incarnation.hosted_force import install_hosted_force
from base.agents.incarnation.resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
    freeze_resources,
)
from base.db import create_agent
from base.native_process.runtime_incarnation import RuntimeIncarnation


def _process(pid: int, birth: float) -> ResourceProcess:
    captured = ResourceProcess.capture(psutil.Process())
    return captured.model_copy(update={"pid": pid, "birth": birth})


def _admitted(conn: psycopg.Connection) -> RuntimeIncarnation:
    target = RuntimeIncarnation(create_agent(conn), uuid4(), uuid4())
    admitted = IncarnationResources(generation=target.generation, owner=target.owner, requests={})
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,incarnation_resources) "
        "VALUES(%s,'idling','resource-test',%s)",
        (target.agent_id, Jsonb(admitted.model_dump(mode="json"))),
    )
    conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s,runtime_kind='hosted',"
        "lease_expires_at=clock_timestamp()+interval '1 minute' WHERE id=%s",
        (target.generation, target.owner, target.agent_id),
    )
    conn.commit()
    return target


def exec_allocation_entry() -> ExecAllocation:
    return ExecAllocation(
        request=uuid4(),
        domain=uuid4(),
        request_digest="a" * 64,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


# meiva:note
# Store an allocation the way a retired writer left it, for the refusals that still read it.
def plant_exec_allocation(
    conn: psycopg.Connection, target: RuntimeIncarnation, entry: ExecAllocation
) -> None:
    row = conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None
    stored = decode_resources(row[0])
    assert isinstance(stored, IncarnationResources)
    planted = stored.model_copy(update={"requests": stored.requests | {str(entry.request): entry}})
    conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(planted.model_dump(mode="json")), target.agent_id),
    )


def force_incarnation(conn: psycopg.Connection, target: RuntimeIncarnation) -> int:
    conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (target.agent_id,))
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,content,source,status) "
        "VALUES(%s,'terminate','','user','pending') RETURNING id",
        (target.agent_id,),
    ).fetchone()
    assert row is not None
    conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user',"
        "lifecycle_command_id=%s WHERE id=%s",
        (row[0], target.agent_id),
    )
    install_hosted_force(conn, target.agent_id, row[0])
    freeze_resources(conn, target, row[0])
    return row[0]


@pytest.mark.parametrize("value", [None, {}, {"version": 2, "state": "admitted"}])
def test_unknown_or_bad_evidence_is_never_empty(value: object) -> None:
    with pytest.raises((ValueError, ResourceEvidenceError)):
        decode_resources(value)


def test_force_freezes_the_stored_set(db_conn: psycopg.Connection) -> None:
    target = _admitted(db_conn)
    entry = exec_allocation_entry()
    with db_conn.transaction():
        plant_exec_allocation(db_conn, target, entry)
        force = force_incarnation(db_conn, target)
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None
    stored = decode_resources(row[0])
    assert isinstance(stored, IncarnationResources)
    assert stored.frozen_by == force and stored.requests == {str(entry.request): entry}
    db_conn.commit()


@pytest.mark.parametrize("wrong_part", ["generation", "owner"])
def test_freeze_requires_the_exact_live_owner(db_conn: psycopg.Connection, wrong_part: str) -> None:
    target = _admitted(db_conn)
    wrong = RuntimeIncarnation(
        target.agent_id,
        uuid4() if wrong_part == "generation" else target.generation,
        uuid4() if wrong_part == "owner" else target.owner,
    )
    with (
        pytest.raises(ResourceEvidenceError, match="lost its runtime incarnation"),
        db_conn.transaction(),
    ):
        freeze_resources(db_conn, wrong, 1)
