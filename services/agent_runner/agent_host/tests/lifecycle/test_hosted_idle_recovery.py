import asyncio

import pytest

from agent.state import BaseAgentState
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.sessions.tests.poll_until import poll_until_async
from services.agent_runner.agent_host.dispatcher import TurnScheduler


def _accept_model(**_kwargs: object) -> None:
    pass


def _unexpected_model(state: BaseAgentState) -> dict[str, object]:
    del state
    pytest.fail("quiet idle recovery reached a model/initialization node")


class TestReapedSuccessorMarker:
    @pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
    async def test_reaped_successor_marker_clears_when_task_finishes(self, outcome: str) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        failure = ValueError("turn failed")

        async def run_turn(_agent_id: int) -> None:
            entered.set()
            await release.wait()
            if outcome == "error":
                raise failure

        scheduler = TurnScheduler(run_turn)
        try:
            original = scheduler.wake(1)
            assert original is not None
            original.cancel()
            await poll_until_async(lambda: scheduler.reaped_successor(1) is not None, timeout=3)
            successor = scheduler.reaped_successor(1)
            assert successor is not None
            await asyncio.wait_for(entered.wait(), 3)
            assert scheduler.reaped_successor(1) is successor
            if outcome == "cancel":
                successor.cancel()
            else:
                release.set()
            await poll_until_async(
                lambda: successor.done() and scheduler.reaped_successor(1) is None, timeout=3
            )
        finally:
            release.set()
            if outcome == "error":
                with pytest.raises(ValueError) as joined:
                    await scheduler.aclose()
                assert joined.value is failure
            else:
                await scheduler.aclose()
