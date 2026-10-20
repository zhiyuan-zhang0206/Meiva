# meiva:note
# The resource-admission protocol: an unknown or unresolved set cannot be reset and a malformed record never downgrades to legacy.

import os
from collections.abc import Iterator
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.resource_admission import admit_resources_async
from base.agents.incarnation.resources import ResourceEvidenceError
from base.agents.incarnation.tests.test_resources import (
    _admitted,
    _process,
    exec_allocation_entry,
    plant_exec_allocation,
)
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


@pytest.fixture(autouse=True)
def restore_process_delivery() -> Iterator[None]:
    """Own cold process-client configuration delivery for this consumer scope."""
    with patch.dict(os.environ):
        yield


async def test_successor_cannot_reset_unknown_or_unresolved_set(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    target = _admitted(db_conn)
    entry = exec_allocation_entry()
    with db_conn.transaction():
        plant_exec_allocation(db_conn, target, entry)
    successor = RuntimeIncarnation(target.agent_id, uuid4(), uuid4())
    with pytest.raises(ResourceEvidenceError):
        async with async_write_transaction(aops_pool) as conn:
            await admit_resources_async(conn, successor, _process(999, 1.0))
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None and str(entry.request) in row[0]["requests"]


async def test_malformed_never_downgrades_to_legacy(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    target = _admitted(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb({"version": 9}), target.agent_id),
    )
    db_conn.commit()
    with pytest.raises(ValueError):
        async with async_write_transaction(aops_pool) as conn:
            await admit_resources_async(conn, target, _process(10, 1.0))
