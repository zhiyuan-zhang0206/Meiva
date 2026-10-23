# meiva:note
# The exec leg of the graph-level cancel fixture, and the one tool-call message its tests share.
#
# `install_cancel_event` is imported by `agent/graph/tests/cancel_fixture.py`, which combines the
# llm, exec and compact subscriptions for the graph-level tests. This package's own tests never
# patch the subscription: a turn without an inbound queue (`ops_pool=None`) runs its exec
# uninterruptibly, and a turn that must be interrupted gets a real pending row on the test
# database.


from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph.exec import node
from agent.graph.interrupt import InterruptEvent
from base.native_process.turn_identity import HostedTurnResources


def install_cancel_event(monkeypatch: pytest.MonkeyPatch, event: InterruptEvent) -> None:
    """Make this package's durable-interrupt subscription yield `event`."""

    @asynccontextmanager
    async def fake_subscribe(
        _pool: AsyncConnectionPool | None,
        _agent_id: int,
        *,
        resources: HostedTurnResources | None,
    ) -> AsyncGenerator[InterruptEvent]:
        yield event

    monkeypatch.setattr(node, "subscribe_interrupt", fake_subscribe)


def ai_with_code(code: str) -> AIMessage:
    """Single tool wire format: AIMessage with execute_code tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "call_test"}],
    )
