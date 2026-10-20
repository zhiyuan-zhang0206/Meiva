# meiva:note
# `POST /cancel-turn`: cancel the agent's running turn task, change no row.

import asyncio
import json

from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.health_routes import cancel_turn_route
from services.agent_runner.agent_host.tests.turn_dispatcher.helpers import settle


async def _post(scheduler: TurnScheduler, body: bytes) -> tuple[int, dict[str, object]]:
    status, response, _ = await cancel_turn_route(scheduler)(body)
    return status, json.loads(response)


async def test_cancels_the_running_turn_and_the_next_wake_starts_a_fresh_one() -> None:
    entered, second_turn = asyncio.Event(), asyncio.Event()
    started: list[int] = []

    async def run(agent_id: int) -> None:
        started.append(agent_id)
        if len(started) == 1:
            entered.set()
            await asyncio.Event().wait()
        else:
            second_turn.set()

    scheduler = TurnScheduler(run)
    try:
        scheduler.wake(4)
        await asyncio.wait_for(entered.wait(), 2)
        assert await _post(scheduler, b'{"agent_id": 4}') == (200, {"cancelled": True})
        await settle()
        assert scheduler.active_agents == frozenset()
        scheduler.wake(4)
        await asyncio.wait_for(second_turn.wait(), 2)
        assert started == [4, 4]
    finally:
        await scheduler.aclose()


async def test_answers_not_cancelled_when_no_turn_is_running() -> None:
    async def run(_agent_id: int) -> None:
        return None

    scheduler = TurnScheduler(run)
    try:
        assert await _post(scheduler, b'{"agent_id": 9}') == (200, {"cancelled": False})
    finally:
        await scheduler.aclose()


async def test_a_cancel_never_reaches_another_agents_turn() -> None:
    entered = asyncio.Event()

    async def run(_agent_id: int) -> None:
        entered.set()
        await asyncio.Event().wait()

    scheduler = TurnScheduler(run)
    try:
        scheduler.wake(1)
        await asyncio.wait_for(entered.wait(), 2)
        assert await _post(scheduler, b'{"agent_id": 2}') == (200, {"cancelled": False})
        assert scheduler.active_agents == frozenset({1})
    finally:
        await scheduler.aclose()


async def test_rejects_a_body_without_a_positive_integer_agent_id() -> None:
    async def run(_agent_id: int) -> None:
        return None

    scheduler = TurnScheduler(run)
    try:
        for body in (
            b"",
            b"not json",
            b'{"agent_id": "4"}',
            b'{"agent_id": 0}',
            b'{"agent_id": true}',
        ):
            status, _ = await _post(scheduler, body)
            assert status == 400, body
    finally:
        await scheduler.aclose()
