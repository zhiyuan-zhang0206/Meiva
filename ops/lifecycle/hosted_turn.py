# meiva:note
# Cancel the agent's running turn through this machine's agent host.

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

_log = logging.getLogger(__name__)


# meiva:note
# The values this machine's agent host is reached by.
#
# `home` is the data home whose browser-mcp socket a terminated agent's pages are released
# through, and `health_port` is the loopback port of the host's health server, which serves
# `POST /cancel-turn`. The entry that serves a lifecycle op reads both once.
@dataclass(frozen=True)
class LocalAgentHost:
    home: Path
    health_port: int


# meiva:note
# Cancel the agent's running turn on this machine's agent host.
#
#     Dials the host's loopback health port (`POST /cancel-turn`); the host cancels the turn
#     task with its bounded unwind and answers whether one was running. The agent's row is not
#     touched.
async def cancel_hosted_turn(agent_id: int, *, host: LocalAgentHost) -> bool:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.post(
            f"http://127.0.0.1:{host.health_port}/cancel-turn",
            json={"agent_id": agent_id},
        )
        resp.raise_for_status()
    return bool(resp.json()["cancelled"])


# meiva:note
# Accelerate a force terminate by cancelling the agent's turn task.
#
#     The terminated row is already committed, so a transport or HTTP-status failure is logged
#     at WARNING with the exception and swallowed: a turn that keeps running ends at its next
#     ownership check.
async def cancel_hosted_turn_best_effort(agent_id: int, *, host: LocalAgentHost) -> None:
    try:
        await cancel_hosted_turn(agent_id, host=host)
    except httpx.HTTPError:
        _log.warning(
            "hosted turn-cancel call for agent %s failed (non-fatal; the agent is "
            "terminated and its running turn ends at the next ownership check)",
            agent_id,
            exc_info=True,
        )


async def prepare_harness_switch(
    agent_id: int,
    *,
    old_harness: str,
    new_harness: str,
    source: str,
    message: str,
    host: LocalAgentHost,
) -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(
                f"http://127.0.0.1:{host.health_port}/prepare-harness-switch",
                json={
                    "agent_id": agent_id,
                    "old_harness": old_harness,
                    "new_harness": new_harness,
                    "source": source,
                    "message": message,
                },
            )
            resp.raise_for_status()
            return True
    except httpx.HTTPError:
        _log.warning(
            "prepare_harness_switch call for agent %s failed (non-fatal)",
            agent_id,
            exc_info=True,
        )
        return False
