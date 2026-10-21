from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from base.api_contracts.agent_contract import Harness
from base.lm.harness import HarnessTurn, RegistrationStore
from base.log.tests.log_records import LogRecords
from services.agent_runner.agent_host.runtime import AgentRuntime, HarnessSessions


class ClosingEngine:
    def __init__(self, error: BaseException | None = None) -> None:
        self.harness = Harness.CODEX
        self.model = None
        self.error = error
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.closed = False

    async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
        raise AssertionError("retirement must not run another turn")

    async def interrupt(self) -> object:
        raise AssertionError("retirement must close the session")

    async def close(self, *, reason: str = "closed") -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        self.closed = True


def runtime(engine: ClosingEngine) -> AgentRuntime:
    return AgentRuntime(
        fingerprint="old",
        llm=Mock(),
        harness=engine.harness,
        harness_bridge=engine,
    )


async def wait_for_retirement(
    owner: HarnessSessions, agent_id: int, entered: asyncio.Event
) -> Exception | None:
    entered.set()
    try:
        await owner.join(agent_id)
    except Exception as error:
        return error
    return None


@pytest.mark.parametrize("fails", [False, True])
async def test_cancelled_join_preserves_the_close_and_the_next_admission_fence(
    tmp_path: Path, fails: bool
) -> None:
    error = RuntimeError("close failed after the waiter left") if fails else None
    engine = ClosingEngine(error)
    owner = HarnessSessions(RegistrationStore(tmp_path))
    await owner.start()
    owner.retire(1, runtime(engine))
    try:
        await engine.started.wait()
        async with asyncio.TaskGroup() as tasks:
            entered = asyncio.Event()
            waiter = tasks.create_task(wait_for_retirement(owner, 1, entered))
            await entered.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not engine.cancelled
            entered = asyncio.Event()
            replacement = tasks.create_task(wait_for_retirement(owner, 1, entered))
            await entered.wait()
            assert not replacement.done(), "a cancelled waiter must leave the fence in place"
            engine.release.set()
            if error is None:
                assert await replacement is None
                await owner.join(1)
                assert engine.closed
                later = ClosingEngine()
                later.release.set()
                owner.retire(1, runtime(later))
                await owner.join(1)
                assert later.closed
            else:
                assert await replacement is error
    finally:
        engine.release.set()
        await owner.aclose()


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_failed_close_keeps_rejecting_joins_without_cancelling_another_agent(
    tmp_path: Path, loguru_records: LogRecords, error_type: type[BaseException]
) -> None:
    error = error_type("CLI transport did not close")
    failed, other = ClosingEngine(error), ClosingEngine()
    owner = HarnessSessions(RegistrationStore(tmp_path))
    await owner.start()
    owner.retire(1, runtime(failed))
    owner.retire(2, runtime(other))
    try:
        await failed.started.wait()
        await other.started.wait()
        with pytest.raises(RuntimeError, match="already has a retiring harness session"):
            owner.retire(1, runtime(failed))
        failed.release.set()
        for _ in range(2):
            with pytest.raises(error_type) as raised:
                await owner.join(1)
            assert raised.value is error
        assert not other.cancelled
        other.release.set()
        await owner.join(2)
        assert other.closed
        assert owner.failures == [error]
        assert len(loguru_records.events("harness_close_failed")) == 1
    finally:
        failed.release.set()
        other.release.set()
        await owner.aclose()
    [summary] = loguru_records.events("harness_close_failures")
    assert "1 session close(s) raised" in summary["message"]
    assert "CLI transport did not close" in summary["message"]
    assert owner.failures == [error]
    with pytest.raises(error_type) as raised:
        await owner.join(1)
    assert raised.value is error
