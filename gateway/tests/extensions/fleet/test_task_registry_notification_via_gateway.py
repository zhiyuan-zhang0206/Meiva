from uuid import uuid4

import psycopg
import pytest

from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentityReader
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.support.db_faults.insert_fault import failing_insert
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import seed_agent

# meiva:note
# The task registry publishes through the event bus of the context that commits; every test here
# pins its agents under a context that carries one.
pytestmark = pytest.mark.usefixtures("pinned_bus")


def test_gateway_enqueue_failure_rolls_back_assignment(
    redis_url: str,
    db_conn: psycopg.Connection,
    root_task_id: int,
    machine_identity: MachineIdentityReader,
) -> None:
    actor = seed_agent(db_conn)
    old_owner = seed_agent(db_conn)
    new_owner = seed_agent(db_conn)
    pin_agent(actor)
    task = task_registry.create(
        "atomic", "work", parent=root_task_id, owner=old_owner, operation_key=str(uuid4())
    )

    with gateway_test_client(raise_server_exceptions=False, machine=machine_identity) as client:
        with failing_insert(db_conn, "inbound_messages", message="queue unavailable"):
            assert (
                client.patch(
                    f"/api/tasks/{task.id}",
                    json={"owner": new_owner},
                    headers=keyed_headers(str(uuid4())),
                ).status_code
                == 500
            )
        with db_conn.cursor() as cur:
            cur.execute("SELECT owner FROM agent_tasks WHERE id=%s", (task.id,))
            assert cur.fetchone() == (old_owner,)
            cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (new_owner,))
            assert cur.fetchone() == (0,)
        assert (
            client.patch(
                f"/api/tasks/{task.id}",
                json={"owner": new_owner},
                headers=keyed_headers(str(uuid4())),
            ).status_code
            == 200
        )
