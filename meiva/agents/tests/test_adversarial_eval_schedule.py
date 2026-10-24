"""The adversarial-eval batch's worker-done predicate.

Pins the 2026-08-26 wedge: freshly spawned workers start as IDLING before
their process boots, and the old predicate (`IDLING or TERMINATED = done`)
read the whole batch as finished seconds after spawning — auditing empty
transcripts while the workers kept running as orphans.
"""

from __future__ import annotations

import importlib
from collections.abc import Generator
from contextlib import contextmanager

import httpx
import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity
from meiva.agents import AgentStatus as S
from meiva.sdk_surface import process_context

# Untyped fixtures and helper calls throughout: the call-site rules stay at warning for this file.
# pyright: reportUnknownArgumentType = warning

# The schedule module's file name carries a hyphen, so plain `from schedules...`
# import cannot reach it — importlib does.
mod = importlib.import_module("schedules.adversarial-eval-weekly-schedule")


def _status(value: str | None) -> S | None:
    return next((member for member in S if member.value == value), None) if value else None


# meiva:note
# A gateway that answers the worker's last-message read, behind the SDK's own transport.
@contextmanager
def _gateway_saying(last_message: str | None) -> Generator[None]:

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/1234/last-message"
        return httpx.Response(200, json={"text": last_message})

    context = MeivaContext(
        identity=AgentIdentity(agent_id=1, owns_loop=True),
        clients=ClientSet(
            gateway_url="http://gateway",
            gateway=lambda url: httpx.Client(base_url=url, transport=httpx.MockTransport(handle)),
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
        yield
    finally:
        try:
            context.clients.close()
        finally:
            if previous is None:
                process_context.unbind_context()
            else:
                process_context.bind_context(previous)


@pytest.mark.parametrize(
    ("status", "last_message", "expected"),
    [
        ("terminated", None, True),  # the normal self-terminated end
        ("terminated", "done report", True),
        ("idling", "done report", True),  # finished a turn but forgot to self-terminate
        ("idling", None, False),  # fresh spawn: row created idling, process not booted
        ("running", None, False),
        ("running", "mid-task note", False),
        (None, None, False),  # row not yet visible to list_agents
    ],
)
def test_worker_done(status: str | None, last_message: str | None, expected: bool) -> None:
    with _gateway_saying(last_message):
        assert mod._worker_done(1234, _status(status)) is expected
