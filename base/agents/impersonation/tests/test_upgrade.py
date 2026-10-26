from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation


def old_lease(
    conn: psycopg.Connection, *, harness: str, status: str, automatic: bool
) -> tuple[str, RuntimeIncarnation]:
    created = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert created is not None
    agent_id = created[0]
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,harness,runtime_generation,runtime_owner) "
        "VALUES(%s,'idling','upgrade-test',%s,%s,%s)",
        (agent_id, harness, owner.generation, owner.owner),
    )
    lease_id = str(uuid4())
    conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,"
        "expires_at,automatic,accepted_generation,accepted_owner,event_delivery_protocol_version) "
        "VALUES(%s,%s,'external_agent:codex','upgrade-test',%s,300,"
        "clock_timestamp()+interval '5 minutes',%s,%s,%s,%s)",
        (
            lease_id,
            agent_id,
            status,
            automatic,
            owner.generation,
            owner.owner,
            2 if automatic else None,
        ),
    )
    return lease_id, owner


@pytest.mark.parametrize("status", ["requested", "accepted", "active"])
def test_upgrade_retires_unsupported_leases_without_losing_the_native_return(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus, status: str
) -> None:
    # meiva:note
    # Recreate rows issued before the admission trigger existed and verify both
    # runtime rejection and the baseline admission trigger contract.
    with db_conn.transaction():
        db_conn.execute(
            "ALTER TABLE agent_impersonations DISABLE TRIGGER agent_impersonations_require_meiva"
        )
        external, external_owner = old_lease(
            db_conn, harness="codex", status=status, automatic=True
        )
        db_conn.execute(
            "ALTER TABLE agent_impersonations ENABLE TRIGGER agent_impersonations_require_meiva"
        )
    db_conn.commit()

    with pytest.raises(leases.ImpersonationError, match="Meiva harness"):
        if status == "requested":
            leases.prepare(
                database,
                event_bus,
                external,
                external_owner.agent_id,
                external_owner,
                "Continue",
                local_machine="upgrade-test",
            )
        elif status == "accepted":
            leases.activate(database, event_bus, external, external_owner)
        else:
            leases.require_active(database, external, local_machine="upgrade-test")

    with (
        pytest.raises(psycopg.errors.RaiseException, match="Manual impersonation consent"),
        db_conn.transaction(),
    ):
        old_lease(db_conn, harness="meiva", status=status, automatic=False)

    with (
        pytest.raises(
            psycopg.errors.RaiseException, match="Impersonation requires the Meiva harness"
        ),
        db_conn.transaction(),
    ):
        old_lease(db_conn, harness="codex", status=status, automatic=True)
