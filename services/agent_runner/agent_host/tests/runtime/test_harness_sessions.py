from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from base.api_contracts.agent_contract import Harness
from base.lm.harness import HarnessTurn, RegistrationStore
from base.lm.harness.acp.transport import AcpConnection
from base.log.tests.log_records import LogRecords
from services.agent_runner.agent_host.runtime import AgentRuntime, HarnessSessions


class ClosingEngine:
    def __init__(self, error: BaseException | None = None, *, exit_code: int | None = None) -> None:
        self.harness = Harness.CODEX
        self.model = None
        self.error = error
        # meiva:note
        # The three states a real engine answers after close (task #5616).
        self.exit_code: int | None = exit_code
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.closed = False

    async def run_turn(self, text: str, *, key: str, on_event: object = None) -> HarnessTurn:
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


# meiva:note
# Collects what the host would write to agents_meta (task #5616).
class ExitRecorder:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int | None]] = []

    async def __call__(self, agent_id: int, exit_code: int | None) -> None:
        self.writes.append((agent_id, exit_code))


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
    owner = HarnessSessions(RegistrationStore(tmp_path), record_exit=ExitRecorder())
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
    owner = HarnessSessions(RegistrationStore(tmp_path), record_exit=ExitRecorder())
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


# meiva:note
# The four states of the recorded exit status (task #5616). Only a close that
# actually finished may write: a session still running, a close that raised, and an
# agent with no harness bridge at all (the built-in meiva loop has no CLI child) must
# all leave the row untouched — the third case is the "never fake a 0" ruling.
@pytest.mark.parametrize("exit_code", [7, 0])
async def test_a_closed_session_records_its_exit_code(tmp_path: Path, exit_code: int) -> None:
    recorder = ExitRecorder()
    engine = ClosingEngine(exit_code=exit_code)
    owner = HarnessSessions(RegistrationStore(tmp_path), record_exit=recorder)
    await owner.start()
    try:
        owner.retire(41, runtime(engine))
        await engine.started.wait()
        assert recorder.writes == [], "a running session has no exit status to record"
        engine.release.set()
        await owner.join(41)
        assert engine.closed
        assert recorder.writes == [(41, exit_code)]
    finally:
        engine.release.set()
        await owner.aclose()


async def test_a_failed_close_records_nothing(tmp_path: Path) -> None:
    recorder = ExitRecorder()
    engine = ClosingEngine(RuntimeError("CLI transport did not close"), exit_code=9)
    owner = HarnessSessions(RegistrationStore(tmp_path), record_exit=recorder)
    await owner.start()
    try:
        owner.retire(42, runtime(engine))
        await engine.started.wait()
        engine.release.set()
        with pytest.raises(RuntimeError, match="did not close"):
            await owner.join(42)
        assert recorder.writes == []
    finally:
        engine.release.set()
        await owner.aclose()


async def test_the_builtin_meiva_loop_never_records_a_zero(tmp_path: Path) -> None:
    recorder = ExitRecorder()
    owner = HarnessSessions(RegistrationStore(tmp_path), record_exit=recorder)
    await owner.start()
    try:
        owner.retire(43, AgentRuntime(fingerprint="builtin", llm=Mock(), harness=Harness.MEIVA))
        await owner.aclose()
        assert recorder.writes == [], "no CLI child exists, so nothing may be recorded as 0"
    finally:
        await owner.aclose()


# meiva:note
# The adapter layer is where "we record the exit status" can still be wrong while the
# recording layer is right, so this drives a real child process — a stub script that
# exits 3 — through the production transport, then through the real `_close`, and
# asserts the recorded value is that status rather than NULL (task #5616).
class TransportBackedEngine:
    def __init__(self, connection: AcpConnection) -> None:
        self.harness = Harness.KIMI_CODE
        self.model = None
        self._connection = connection
        self.closed = False

    async def run_turn(self, text: str, *, key: str, on_event: object = None) -> HarnessTurn:
        raise AssertionError("this engine is never asked for a turn")

    async def interrupt(self) -> object:
        raise AssertionError("this engine is never interrupted")

    async def close(self, *, reason: str = "closed") -> None:
        await self._connection.close(reason=reason)
        self.closed = True

    @property
    def exit_code(self) -> int | None:
        return self._connection.returncode


async def test_a_real_child_exit_status_reaches_the_recorder(tmp_path: Path) -> None:
    stub = tmp_path / "stub-cli"
    stub.write_text("#!/bin/sh\nexit 3\n")
    stub.chmod(0o755)
    connection = AcpConnection([str(stub)], cwd=str(tmp_path), environ={})
    await connection.start()
    recorder = ExitRecorder()
    owner = HarnessSessions(RegistrationStore(tmp_path), record_exit=recorder)
    await owner.start()
    try:
        owner.retire(44, runtime(TransportBackedEngine(connection)))
        await owner.join(44)
        assert recorder.writes == [(44, 3)]
    finally:
        await owner.aclose()


async def test_a_running_session_records_nothing(tmp_path: Path) -> None:
    recorder = ExitRecorder()
    engine = ClosingEngine(exit_code=7)
    owner = HarnessSessions(RegistrationStore(tmp_path), record_exit=recorder)
    await owner.start()
    try:
        owner.retire(45, runtime(engine))
        await engine.started.wait()
        assert recorder.writes == []
    finally:
        engine.release.set()
        await owner.aclose()
