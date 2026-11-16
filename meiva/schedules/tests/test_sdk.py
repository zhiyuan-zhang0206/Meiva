from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity
from meiva import gateway_client, schedules
from meiva.sdk_surface import process_context


def _row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "name": "nightly",
        "description": None,
        "script": "print('hi')\n",
        "enabled": True,
        "status": "scheduled",
        "last_error": None,
        "created_at": "2026-11-17T00:00:00Z",
        "updated_at": "2026-11-17T00:00:00Z",
        "machine": "macmini",
        "owner_agent_id": 42,
        "trigger_kind": "every",
        "run_at": None,
        "cron_expr": None,
        "interval_seconds": 60,
        "timezone": "Asia/Shanghai",
        "timeout_seconds": 600,
        "on_owner_terminate": "cancel",
        "next_launch_at": "2026-11-17T00:01:00Z",
    }
    row.update(changes)
    return row


@contextmanager
def _gateway() -> Generator[list[tuple[str, str, object | None]]]:
    sent: list[tuple[str, str, object | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = None if not request.content else json.loads(request.content)
        sent.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/api/schedules":
            return httpx.Response(200, json=[_row()])
        return httpx.Response(201 if request.method == "POST" else 200, json=_row())

    context = MeivaContext(
        identity=AgentIdentity(agent_id=42, owns_loop=True),
        clients=ClientSet(
            gateway_url="http://gateway",
            factories={
                GatewayTransportInputs: lambda: GatewayTransportInputs(
                    max_retries_reader=lambda: 1,
                    retry_delay_reader=lambda: 0.0,
                    memory_deadline_reader=lambda: 1.0,
                )
            },
        ),
    )
    previous = process_context.bound_context()
    process_context.bind_context(context)
    try:
        with (
            httpx.Client(
                base_url="http://gateway", transport=httpx.MockTransport(handle)
            ) as client,
            gateway_client.use_client(client),
        ):
            yield sent
    finally:
        try:
            context.clients.close()
        finally:
            if previous is None:
                process_context.unbind_context()
            else:
                process_context.bind_context(previous)


def test_every_defaults_owner_to_the_current_agent() -> None:
    with _gateway() as sent:
        row = schedules.every(60, name="nightly", script="print('hi')\n")

    assert row.id == 7
    assert sent[0][0:2] == ("POST", "/api/schedules")
    assert sent[0][2] == {
        "name": "nightly",
        "trigger_kind": "every",
        "timeout_seconds": 600,
        "enabled": True,
        "script": "print('hi')\n",
        "interval_seconds": 60,
        "owner_agent_id": 42,
    }


def test_at_serializes_timezone_aware_instant_and_system_owner() -> None:
    instant = datetime(2026, 11, 17, 12, 30, tzinfo=UTC)
    with _gateway() as sent:
        schedules.at(
            instant,
            name="once",
            message="wake up",
            target_agent_id=9,
            owner_agent_id=None,
        )

    body = sent[0][2]
    assert isinstance(body, dict)
    assert body["trigger_kind"] == "at"
    assert body["run_at"] == instant.isoformat()
    assert body["message"] == "wake up"
    assert body["target_agent_id"] == 9
    assert "owner_agent_id" not in body


def test_list_get_and_lifecycle_actions_use_first_class_routes() -> None:
    with _gateway() as sent:
        assert schedules.list()[0].name == "nightly"
        assert schedules.get(7).id == 7
        schedules.disable(7)
        schedules.enable(7)
        schedules.run_now(7)
        schedules.cancel(7)

    assert [(method, path) for method, path, _body in sent] == [
        ("GET", "/api/schedules"),
        ("GET", "/api/schedules/7"),
        ("POST", "/api/schedules/7/disable"),
        ("POST", "/api/schedules/7/enable"),
        ("POST", "/api/schedules/7/run-now"),
        ("POST", "/api/schedules/7/cancel"),
    ]


def test_sdk_rejects_invalid_local_inputs_before_transport() -> None:
    # meiva:note
    # A naive datetime is intentional input to the SDK validation boundary.
    with _gateway() as sent:
        with pytest.raises(ValueError, match="at least 30"):
            schedules.every(29, name="too-fast", script="pass")
        with pytest.raises(ValueError, match="timezone-aware"):
            schedules.at(
                datetime(2026, 11, 17),  # noqa: DTZ001
                name="naive",
                script="pass",
            )
        with pytest.raises(ValueError, match="exactly one"):
            schedules.cron("0 * * * *", name="ambiguous", script="pass", message="wake")

    assert sent == []
