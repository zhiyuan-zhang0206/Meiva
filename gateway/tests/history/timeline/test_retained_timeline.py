from __future__ import annotations

import secrets

import psycopg
import pytest
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from base.config import settings
from base.db import create_agent
from base.host.net.auth import bearer_header
from gateway.tests.agents.gateway_auth import without_credentials
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.history.test_timeline import CompactHistoryCases
from gateway.tests.history.test_timeline import test_client as test_client
from gateway.tests.support.checkpoints.unreadable import put_unreadable_head


def _put_boundary(agent_id: int, name: str, *, version: str) -> str:
    messages: list[BaseMessage] = [
        SystemMessage(content="system"),
        *(AIMessage(content=f"{name} {i}", id=f"{name}{i}") for i in range(3)),
    ]
    return CompactHistoryCases._put_checkpoint(agent_id, messages, version=version, boundary=True)


def test_retained_read_requires_auth_and_serializes_for_authenticated_caller(
    db_conn: psycopg.Connection,
) -> None:
    secret = secrets.token_urlsafe(24)
    tid = create_agent(db_conn)
    with gateway_test_client(cluster_secret=secret) as client:
        rejected = without_credentials(client).get(f"/api/agents/{tid}/timeline/retained")
        assert rejected.status_code == 401
        accepted = client.get(f"/api/agents/{tid}/timeline/retained", headers=bearer_header(secret))
    assert accepted.status_code == 200
    assert accepted.json() == {"boundary_checkpoint_id": None, "items": [], "has_more": False}


def test_retained_history_can_start_and_page_without_reading_broken_live_head(
    db_conn: psycopg.Connection, test_client: GatewayTestClient
) -> None:
    tid = create_agent(db_conn)
    test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = -1
    older = _put_boundary(tid, "older", version="1")
    newer = _put_boundary(tid, "newer", version="2")
    # meiva:note
    # The live head declares messages it cannot deserialize: retained reads must not touch it.
    put_unreadable_head(settings.data_plane.db_url, tid)

    live = test_client.get(f"/api/agents/{tid}/timeline")
    assert live.status_code == 503
    first = test_client.get(f"/api/agents/{tid}/timeline/retained", params={"limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert body["boundary_checkpoint_id"] == newer
    assert "msg_count" not in body
    assert [item["item_id"] for item in body["items"]] == [f"s1.{newer}.2.0"]
    assert body["has_more"] is True
    second = test_client.get(
        f"/api/agents/{tid}/timeline/retained",
        params={"limit": 1, "before": body["items"][0]["item_id"]},
    )
    assert second.status_code == 200
    assert [item["item_id"] for item in second.json()["items"]] == [f"s1.{newer}.1.0"]
    explicit = test_client.get(
        f"/api/agents/{tid}/timeline/retained", params={"checkpoint_id": older, "limit": 1}
    )
    assert explicit.status_code == 200
    assert explicit.json()["boundary_checkpoint_id"] == older
    assert [item["item_id"] for item in explicit.json()["items"]] == [f"s2.{older}.2.0"]


@pytest.mark.parametrize(
    ("params", "status"),
    [({"checkpoint_id": "not-retained"}, 404), ({"before": "1.0"}, 400)],
)
def test_retained_entry_never_guesses_a_boundary_or_current_cursor(
    db_conn: psycopg.Connection,
    test_client: GatewayTestClient,
    params: dict[str, str],
    status: int,
) -> None:
    tid = create_agent(db_conn)
    test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = -1
    response = test_client.get(f"/api/agents/{tid}/timeline/retained", params=params)
    assert response.status_code == status


def test_retained_empty_agent_remains_successful_empty(
    db_conn: psycopg.Connection, test_client: GatewayTestClient
) -> None:
    tid = create_agent(db_conn)
    response = test_client.get(f"/api/agents/{tid}/timeline/retained")
    assert response.status_code == 200
    assert response.json() == {"boundary_checkpoint_id": None, "items": [], "has_more": False}
