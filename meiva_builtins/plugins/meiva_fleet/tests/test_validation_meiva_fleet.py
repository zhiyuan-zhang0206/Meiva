"""Argument validation of the fleet plugin's task and notice entry points."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from meiva.tests.fixtures.gateway_wire import GatewayWire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.gateway_wire import wire as wire
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import seed_agent

# meiva:note
# These tests drive the fleet task registry, whose board refreshes publish through the event
# bus of the context that commits, so each runs under a context that carries one.
pytestmark = pytest.mark.usefixtures("pinned_bus")


class TestTasksEntries:
    def test_create_title_unwraps(
        self, db_conn: psycopg.Connection, redis_url: str, root_task_id: int
    ) -> None:
        """Single-element title tuple lands as the unwrapped string (DB round-trip)."""
        from meiva_builtins.plugins.meiva_fleet import task_registry

        pin_agent(seed_agent(db_conn))

        inputs: dict[str, Any] = {
            "title": ("My Task",),
            "description": "d",
            "parent": root_task_id,
            "operation_key": str(uuid4()),
        }
        task = task_registry.create(**inputs)
        assert task.title == "My Task"

    def test_create_multi_element_title_type_errors(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="title must be a string"):
            inputs: dict[str, Any] = {
                "title": ("a", "b"),
                "description": "d",
                "parent": 1,
                "operation_key": str(uuid4()),
            }
            task_registry.create(**inputs)

    def test_create_parent_never_unwraps(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="parent must be int"):
            inputs: dict[str, Any] = {
                "title": "t",
                "description": "d",
                "parent": (1,),
                "operation_key": str(uuid4()),
            }
            task_registry.create(**inputs)

    def test_log_message_unwraps(
        self, db_conn: psycopg.Connection, redis_url: str, root_task_id: int
    ) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        pin_agent(seed_agent(db_conn))
        task = task_registry.create(
            "log target", "d", parent=root_task_id, operation_key=str(uuid4())
        )

        message: Any = ("note",)
        task_registry.log(task.id, message, operation_key=str(uuid4()))

        assert (task_registry.get(task.id).results or "").endswith(" note\n\n")

    def test_log_multi_element_type_errors(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="message must be a string"):
            task_registry.log(7, ("a", "b"), operation_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_update_status_unwraps(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        # Coercion fires before the DB write: a status tuple unwraps, then the
        # status-value validation runs on the string.
        with pytest.raises(ValueError, match="status must be one of"):
            task_registry.update(7, status=("not-a-status",), operation_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_update_owner_never_unwraps(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="owner must be int"):
            task_registry.update(7, owner=(5,), operation_key=str(uuid4()))  # pyright: ignore[reportArgumentType]


class TestNoticeEntries:
    def test_notify_title_unwraps(self, wire: GatewayWire) -> None:
        from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

        wire.stub.reply(
            "POST",
            "/api/agents/900001/notices",
            {"id": 1, "pending_count": 0, "superseded": [], "pending_notices": []},
            status=201,
        )
        pin_agent(900001, clients=wire.context.clients)
        inputs: dict[str, Any] = {
            "title": ("Hi",),
            "content": ("detail",),
            "priority": ("P2",),
            "idempotency_key": "notice-intent",
        }
        notice = fleet_plugin.notify(**inputs)
        [sent] = wire.sent
        assert (sent.method, sent.path) == ("POST", "/api/agents/900001/notices")
        assert sent.body["title"] == "Hi"
        assert sent.body["content"] == "detail"
        assert sent.body["priority"] == "P2"
        assert sent.headers["idempotency-key"] == "notice-intent"
        assert notice == 1

    def test_notify_multi_element_title_type_errors(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="title must be a string"):
            fleet_plugin.notify(("a", "b"), idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_notify_task_never_unwraps(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="task must be int"):
            fleet_plugin.notify("hi", task=("5",), idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]


# meiva:note
# The SDK still sends the real gateway request. Its relative expiration is encoded as one
# absolute UTC deadline; a negative interval is rejected before the listener sees anything.
def test_relative_notice_expiration_reaches_the_gateway(wire: GatewayWire) -> None:
    from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

    wire.stub.reply(
        "POST",
        "/api/agents/900001/notices",
        {"id": 1, "pending_count": 0, "superseded": [], "pending_notices": []},
        status=201,
    )
    pin_agent(900001, clients=wire.context.clients)
    interval = timedelta(minutes=5)
    before = datetime.now(UTC)
    assert fleet_plugin.notify("expires", expire_at=interval, idempotency_key="notice-expiry") == 1
    after = datetime.now(UTC)
    [received] = wire.stub.received
    due = datetime.fromisoformat(received.body["expire_at"])
    assert before + interval <= due <= after + interval
    assert received.headers["idempotency-key"] == "notice-expiry"


def test_past_notice_expiration_is_rejected_without_http(wire: GatewayWire) -> None:
    from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

    pin_agent(900001, clients=wire.context.clients)
    with pytest.raises(ValueError, match="expire_at is in the past"):
        fleet_plugin.notify("expired", expire_at=timedelta(seconds=-1), idempotency_key="expired")
    assert wire.stub.received == []


# meiva:note
# Equality passes the client's existing past-time comparison, but the server evaluates
# expiration at arrival. A real HTTP refusal remains an SDK ValueError, not success.
def test_zero_notice_expiration_preserves_a_gateway_refusal(wire: GatewayWire) -> None:
    from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

    wire.stub.reply(
        "POST", "/api/agents/900001/notices", {"detail": "expire_at is in the past"}, status=422
    )
    pin_agent(900001, clients=wire.context.clients)
    before = datetime.now(UTC)
    with pytest.raises(ValueError, match="expire_at is in the past"):
        fleet_plugin.notify(
            "immediate expiry", expire_at=timedelta(0), idempotency_key="zero-expiry"
        )
    [received] = wire.stub.received
    due = datetime.fromisoformat(received.body["expire_at"])
    assert before <= due <= datetime.now(UTC)
