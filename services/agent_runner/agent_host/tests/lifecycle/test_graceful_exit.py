# meiva:note
# A SIGTERMed agent-host leaves each running turn a durable restart, on real Postgres. The
# turns run the production graph, claim node, settlement and checkpointer; only the model is
# a test double, reached through the ordinary `llm_override` seam. A restart interrupts what
# its turn is doing only once it has waited its grace, which these tests grant by ageing the
# restart row instead of waiting.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

import asyncio
import json
import signal
import subprocess
import sys
from typing import Any

import psycopg
from langchain_core.messages import BaseMessage

from base.agents.observation.turn_progress import TurnProgress
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.log.tests.log_records import LogRecords
from services.agent_runner.agent_host.lifecycle.graceful_exit import (
    RESTART_SOURCE,
    GracefulExit,
    OwedRestart,
)
from services.agent_runner.agent_host.tests.lifecycle.restart_cluster import (
    Cluster,
    Host,
    dispatch_forever,
)
from services.agent_runner.agent_host.tests.lifecycle.restart_cluster import (
    cluster as cluster,
)
from services.agent_runner.agent_host.tests.lifecycle.scripted_model import Script, Step

_ACCEPTED = f"Restart was accepted from {RESTART_SOURCE}"
_RESUMED = f"{_ACCEPTED} while your turn was unfinished."


async def _in_flight_turn(
    cluster: Cluster, *, drain_s: float, steps: list[Step] | None = None
) -> tuple[int, Script, Host]:
    agent_id, script = cluster.spawn(chat="hello", steps=steps)
    served = await cluster.host(drain_s=drain_s, wakes=True)
    served.scheduler.wake(agent_id)
    await asyncio.wait_for(script.entered.wait(), 30)
    return agent_id, script, served


async def _until(condition: Any, *, seconds: float = 10) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.05)


# meiva:note
# The poll interrupted the call and dropped its half sentence; the claim took the restart, the
# host applied it, and the same transaction queued the note that continues the turn.
async def _assert_applied_with_a_continuation(
    cluster: Cluster, agent_id: int, script: Script, first: Host
) -> list[BaseMessage]:
    assert cluster.inbound(agent_id, "restart")[0].status == "done"
    [note] = cluster.inbound(agent_id, "system_note")
    assert (note.source, note.status) == (RESTART_SOURCE, "pending")
    assert _RESUMED in note.content
    assert cluster.row(agent_id) == ("idling", None)
    before = await first.messages(agent_id)
    assert not any("half a sentence" in str(m.content) for m in before)
    assert script.calls == 1
    return before


# meiva:note
# The pending note wakes the agent on the next host, which claims it and runs the model again
# from the same checkpoint.
async def _assert_next_host_resumes(
    cluster: Cluster, agent_id: int, script: Script, before: list[BaseMessage]
) -> None:
    assert await cluster.woken_by_a_new_host(agent_id)
    second = await cluster.host()
    await asyncio.wait_for(second.host.run_turn(agent_id), 30)
    assert script.calls == 2
    assert sum(_RESUMED in str(m.content) for m in script.histories[1]) == 1
    after = await second.messages(agent_id)
    assert [m.content for m in after[: len(before)]] == [m.content for m in before]
    assert str(after[-1].content) == "resumed"
    assert [n.status for n in cluster.inbound(agent_id, "system_note")] == ["done"]
    assert cluster.row(agent_id)[0] == "idling"
    await second.close()


async def test_sigterm_interrupts_a_turn_once_its_restart_has_waited_and_the_next_host_resumes_it(
    cluster: Cluster,
) -> None:
    agent_id, script, first = await _in_flight_turn(
        cluster,
        drain_s=30,
        steps=[Step(partial="half a sentence", holds=True), Step(text="resumed")],
    )
    serving = first.serve()
    # meiva:note
    # A wake that lands mid-turn would loop the agent into another turn; the exit must not
    # start one.
    first.scheduler.wake(agent_id)

    first.graceful.request()
    await _until(lambda: cluster.inbound(agent_id, "restart"))
    [restart] = cluster.inbound(agent_id, "restart")
    assert (restart.source, restart.status) == (RESTART_SOURCE, "pending")
    # meiva:note
    # The restart has not waited its grace: the held model call is not interrupted.
    await asyncio.sleep(0.5)
    assert not serving.done() and first.scheduler.active_agents == frozenset({agent_id})

    cluster.let_grace_pass(restart.id)
    await asyncio.wait_for(serving, 30)
    assert first.scheduler.active_agents == frozenset()
    await first.close()

    before = await _assert_applied_with_a_continuation(cluster, agent_id, script, first)
    await _assert_next_host_resumes(cluster, agent_id, script, before)


async def test_a_turn_past_the_drain_budget_is_named_and_its_restart_waits_for_the_next_host(
    cluster: Cluster, loguru_records: LogRecords
) -> None:
    agent_id, script, first = await _in_flight_turn(cluster, drain_s=0.5)
    serving = first.serve()
    first.graceful.request()
    await asyncio.wait_for(serving, 30)

    # meiva:note
    # The budget ended before the restart's grace did, so the call is still held: the turn
    # is named at ERROR and left to the cancel path. Its restart is durable and the next
    # host's scan wakes the agent.
    [timeout] = loguru_records.events("host_exit_drain_timeout")
    assert timeout["level"].name == "ERROR"
    assert timeout["extra"]["agent_id"] == agent_id
    [restart] = cluster.inbound(agent_id, "restart")
    assert restart.status == "pending" and timeout["extra"]["restart"] == restart.id
    assert await cluster.woken_by_a_new_host(agent_id)

    # meiva:note
    # A cut-short turn is not resumed: the successor's first claim takes the restart as an
    # idle agent's, with its acceptance marker and no continuation note.
    second = await cluster.host()
    await asyncio.wait_for(second.host.run_turn(agent_id), 30)
    assert cluster.inbound(agent_id, "restart")[0].status == "done"
    assert cluster.inbound(agent_id, "system_note") == []
    assert cluster.row(agent_id) == ("idling", None)
    assert script.calls == 1
    accepted = [
        str(m.content) for m in await second.messages(agent_id) if _ACCEPTED in str(m.content)
    ]
    assert len(accepted) == 1 and "unfinished" not in accepted[0]

    # meiva:note
    # The first host's own cleanup, run here only to end the test, is fenced off the row.
    await first.close()
    assert cluster.row(agent_id) == ("idling", None)
    await second.close()


async def test_an_unfinished_lifecycle_command_is_kept_instead_of_adding_a_restart(
    cluster: Cluster, loguru_records: LogRecords
) -> None:
    agent_id, script, first = await _in_flight_turn(cluster, drain_s=30)
    operator = cluster.send(agent_id, "", kind="restart")
    serving = first.serve()
    first.graceful.request()
    await _until(lambda: loguru_records.events("host_exit_draining"))
    # meiva:note
    # The turn finishes on its own inside the grace, so the operator's restart finds it idle.
    script.release.set()
    await asyncio.wait_for(serving, 30)
    await first.close()

    [draining] = loguru_records.events("host_exit_draining")
    assert (draining["extra"]["written"], draining["extra"]["reused"]) == (0, 1)
    [only] = cluster.inbound(agent_id, "restart", "terminate")
    assert (only.id, only.source, only.status) == (operator, "user", "done")
    assert cluster.inbound(agent_id, "system_note") == []
    assert cluster.row(agent_id) == ("idling", None)


class _Turns:
    def __init__(self, tasks: dict[int, asyncio.Task[None]]) -> None:
        self._tasks = tasks

    def task_for(self, agent_id: int) -> asyncio.Task[None] | None:
        return self._tasks.get(agent_id)

    def stop_admission(self) -> frozenset[int]:
        return frozenset(self._tasks)


async def _drain_with(
    write_restart: Any, *, drain_s: float
) -> tuple[dict[int, asyncio.Task[None]], float]:
    tasks = {agent: asyncio.create_task(dispatch_forever()) for agent in (1, 2)}
    graceful = GracefulExit(
        turns=_Turns(tasks), progress=TurnProgress(), write_restart=write_restart, drain_s=drain_s
    )
    graceful.request()
    started = asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(graceful.drain(), drain_s + 5)
    finally:
        for task in tasks.values():
            task.cancel()
    return tasks, asyncio.get_running_loop().time() - started


async def test_a_failed_restart_write_is_named_and_the_turn_still_waited_for(
    loguru_records: LogRecords,
) -> None:
    async def write_restart(agent_id: int) -> OwedRestart:
        if agent_id == 1:
            raise psycopg.OperationalError("database went away")
        return OwedRestart(20, written=True)

    await _drain_with(write_restart, drain_s=0.2)
    [failed] = loguru_records.events("host_exit_restart_failed")
    assert failed["level"].name == "ERROR" and failed["extra"]["agent_id"] == 1
    assert "database went away" in failed["extra"]["reason"]
    assert {
        r["extra"]["agent_id"]: r["extra"]["restart"]
        for r in loguru_records.events("host_exit_drain_timeout")
    } == {1: None, 2: 20}


async def test_restart_writes_share_the_drain_deadline(loguru_records: LogRecords) -> None:
    async def write_restart(agent_id: int) -> OwedRestart:
        await asyncio.Event().wait()
        raise AssertionError(f"the write for agent {agent_id} never returns")

    _, elapsed = await _drain_with(write_restart, drain_s=0.3)
    assert elapsed < 2
    assert sorted(
        r["extra"]["agent_id"] for r in loguru_records.events("host_exit_restart_failed")
    ) == [1, 2]
    assert all(
        r["extra"]["reason"] == "the drain deadline passed first"
        for r in loguru_records.events("host_exit_restart_failed")
    )
    assert len(loguru_records.events("host_exit_drain_timeout")) == 2


async def test_an_idle_agent_gets_no_restart(cluster: Cluster, loguru_records: LogRecords) -> None:
    agent_id, script = cluster.spawn(chat=None)
    served = await cluster.host()
    serving = served.serve()
    served.graceful.request()
    await asyncio.wait_for(serving, 5)
    await served.close()

    assert cluster.inbound(agent_id, "restart") == []
    assert script.calls == 0
    assert cluster.row(agent_id) == ("idling", None)
    assert [r["extra"]["written"] for r in loguru_records.events("host_exit_draining")] == [0]
    assert len(loguru_records.events("host_exit_drained")) == 1


def test_a_real_sigterm_drains_instead_of_interrupting_and_a_second_one_is_ignored() -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "services.agent_runner.agent_host.tests.lifecycle.child_graceful_exit",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        child.send_signal(signal.SIGTERM)
        assert child.stdout.readline().strip() == "draining"
        child.send_signal(signal.SIGTERM)
        output, errors = child.communicate(timeout=30)
    finally:
        child.kill()
    assert child.returncode == 0, errors
    assert json.loads(output.strip().splitlines()[-1]) == {
        "restarts_for": [7],
        "turn_settled": True,
        "repeats": 1,
    }
