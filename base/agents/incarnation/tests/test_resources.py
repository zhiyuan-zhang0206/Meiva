from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.agents.incarnation.resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
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


@pytest.mark.parametrize("value", [None, {}, {"version": 2, "state": "admitted"}])
def test_unknown_or_bad_evidence_is_never_empty(value: object) -> None:
    with pytest.raises((ValueError, ResourceEvidenceError)):
        decode_resources(value)
