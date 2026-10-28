# meiva:note
# The panel read as the browser sees it: the route names each refusal on the wire
# (`code`), so the panel picks its words from the server's class instead of the
# status, and only an unreachable machine says the same request may pass.
# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md

from collections.abc import Iterator

import psycopg
import pytest

from base.db import create_agent
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable


# meiva:note
# The route asks the cluster client for the read, so the test provides that one
# collaborator and leaves the route itself untouched.
class _RefusingRpc:
    def __init__(self, outcome: Exception) -> None:
        self.outcome = outcome

    async def dispatch_to_machine(
        self,
        db: object,
        target_machine: str,
        kind: str,
        payload: dict[str, object],
        *,
        timeout_s: float | None = None,
        retries: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        raise self.outcome


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    """A gateway app whose pool runs on the test data plane the app opens at boot."""
    with gateway_test_client() as value:
        yield value


def _home_machine_agent(conn: psycopg.Connection, machine: str) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status) VALUES (%s, %s, 'idling')",
        (agent, machine),
    )
    conn.commit()
    return agent


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        ("too_large", 413, "to_user_document_too_large"),
        ("gone", 404, "to_user_reference_gone"),
        ("unreadable", 422, "to_user_document_unreadable"),
    ],
)
def test_a_refused_read_names_its_class_and_is_never_retryable(
    client: GatewayTestClient, db_conn: psycopg.Connection, reason: str, status: int, code: str
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    client.app.state.rpc = _RefusingRpc(
        ClusterOpFailed({"error": "ValueError: refused", "reason": reason})
    )
    response = client.get(f"/api/agents/{agent}/to-user?reference={'a' * 64}")
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert body["retryable"] is False


def test_an_unreachable_machine_stays_the_retryable_answer(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    client.app.state.rpc = _RefusingRpc(ClusterOpUnreachable("dial failed"))
    response = client.get(f"/api/agents/{agent}/to-user")
    assert response.status_code == 503
    assert response.json()["retryable"] is True


def test_a_failure_without_a_reason_is_the_unreadable_class(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    client.app.state.rpc = _RefusingRpc(ClusterOpFailed({"error": "RuntimeError: unexpected"}))
    response = client.get(f"/api/agents/{agent}/to-user")
    assert response.status_code == 422
    assert response.json()["code"] == "to_user_document_unreadable"
