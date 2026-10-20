# meiva:note
# A restart that ends an unfinished turn continues it: the apply queues one pending note in
# the transaction that executes the restart, and the pending scan wakes the agent on whichever
# host runs next. On real Postgres, through the production graph, claim node and host; only
# the model is a test double. A restart interrupts the call or code run in flight only after
# it has waited its grace, which these tests grant by ageing the restart row.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md

import asyncio
import pathlib
from collections.abc import Callable

import psutil
from langchain_core.messages import BaseMessage, ToolMessage

from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.log.tests.log_records import LogRecords
from base.native_process.stop_timing import INTERRUPT_POLL_S
from services.agent_runner.agent_host.tests.lifecycle.restart_cluster import Cluster
from services.agent_runner.agent_host.tests.lifecycle.restart_cluster import cluster as cluster
from services.agent_runner.agent_host.tests.lifecycle.scripted_model import Script, Step

_ACCEPTED = "Restart was accepted from"


# meiva:note
# Code that says it is working, records its process id, and then waits for the test to let it
# go before it says `ending`. A test starts the restart while the code waits.
def _waiting_code(
    tmp_path: pathlib.Path, *, then: str = ""
) -> tuple[pathlib.Path, pathlib.Path, str]:
    started, go = tmp_path / "started", tmp_path / "go"
    code = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\n"
        "print('working', flush=True)\n"
        f"go = pathlib.Path({str(go)!r})\n"
        "end = time.monotonic() + 120\n"
        "while not go.exists() and time.monotonic() < end:\n"
        "    time.sleep(0.05)\n"
        "print('ending')\n"
        f"{then}"
    )
    return started, go, code


async def _until(condition: Callable[[], object], *, seconds: float = 60) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.05)


def _tool_results(messages: list[BaseMessage]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, ToolMessage)]


def _notes(cluster: Cluster, agent_id: int) -> list[str]:
    return [n.content for n in cluster.inbound(agent_id, "system_note") if n.status == "pending"]


# meiva:note
# A new host is woken by the pending note and runs the model again from the checkpoint.
async def _next_host_continues(
    cluster: Cluster, agent_id: int, script: Script
) -> list[BaseMessage]:
    assert await cluster.woken_by_a_new_host(agent_id)
    calls = script.calls
    second = await cluster.host()
    await asyncio.wait_for(second.host.run_turn(agent_id), 60)
    assert script.calls == calls + 1
    assert [n.status for n in cluster.inbound(agent_id, "system_note")] == ["done"]
    assert cluster.row(agent_id)[0] == "idling"
    messages = await second.messages(agent_id)
    assert str(messages[-1].content) == "resumed"
    await second.close()
    return script.histories[-1]


async def test_code_that_ends_inside_the_grace_is_not_interrupted_and_the_restart_lands_after_it(
    cluster: Cluster, tmp_path: pathlib.Path
) -> None:
    started, go, code = _waiting_code(tmp_path)
    agent_id, script = cluster.spawn(chat="go", steps=[Step(code=code), Step(text="resumed")])
    host = await cluster.host()
    turn = host.start_turn(agent_id)
    await _until(started.exists)

    restart = cluster.send(agent_id, "", kind="restart")
    # meiva:note
    # A whole poll interval passes with the restart younger than its grace: the code runs on.
    await asyncio.sleep(INTERRUPT_POLL_S + 0.5)
    assert not turn.done()
    go.write_text("go")
    await asyncio.wait_for(turn, 60)

    # meiva:note
    # The restart took effect at the boundary after the code; the model was not asked again.
    [result] = _tool_results(await host.messages(agent_id))
    assert "working" in result and "ending" in result and "interrupted" not in result
    assert [r.status for r in cluster.inbound(agent_id, "restart")] == ["done"]
    assert cluster.row(agent_id) == ("idling", None)
    assert script.calls == 1
    [note] = _notes(cluster, agent_id)
    assert note.startswith(f"{_ACCEPTED} user while your turn was unfinished.")
    assert restart == cluster.inbound(agent_id, "restart")[0].id
    await host.close()

    history = await _next_host_continues(cluster, agent_id, script)
    assert "ending" in _tool_results(history)[0]
    assert sum(note in str(m.content) for m in history) == 1


async def test_code_past_the_grace_is_killed_and_its_result_says_the_restart_ended_it(
    cluster: Cluster, tmp_path: pathlib.Path
) -> None:
    started, _go, code = _waiting_code(tmp_path)
    agent_id, script = cluster.spawn(chat="go", steps=[Step(code=code), Step(text="resumed")])
    host = await cluster.host()
    turn = host.start_turn(agent_id)
    await _until(started.exists)
    code_pid = int(started.read_text())

    restart = cluster.send(agent_id, "", kind="restart")
    cluster.let_grace_pass(restart)
    await asyncio.wait_for(turn, 60)

    # meiva:note
    # The output so far stays, the marker names the restart, the child is gone, and the
    # remaining step of the turn is owed: the note that continues it was queued with the apply.
    [result] = _tool_results(await host.messages(agent_id))
    assert "working" in result and "ending" not in result
    assert "[interrupted by restart]" in result and "cancelled by" not in result
    await _until(lambda: not psutil.pid_exists(code_pid), seconds=10)
    assert [r.status for r in cluster.inbound(agent_id, "restart")] == ["done"]
    assert cluster.row(agent_id) == ("idling", None)
    assert script.calls == 1
    assert len(_notes(cluster, agent_id)) == 1
    await host.close()

    history = await _next_host_continues(cluster, agent_id, script)
    assert "[interrupted by restart]" in _tool_results(history)[0]


async def test_a_model_call_past_the_grace_is_dropped_and_the_turn_resumes(
    cluster: Cluster,
) -> None:
    agent_id, script = cluster.spawn(
        chat="go", steps=[Step(partial="half a sentence", holds=True), Step(text="resumed")]
    )
    host = await cluster.host()
    turn = host.start_turn(agent_id)
    await asyncio.wait_for(script.entered.wait(), 60)

    restart = cluster.send(agent_id, "", kind="restart")
    cluster.let_grace_pass(restart)
    await asyncio.wait_for(turn, 60)

    messages = await host.messages(agent_id)
    assert not any("half a sentence" in str(m.content) for m in messages)
    assert [r.status for r in cluster.inbound(agent_id, "restart")] == ["done"]
    assert cluster.row(agent_id) == ("idling", None)
    assert script.calls == 1
    assert len(_notes(cluster, agent_id)) == 1
    await host.close()

    history = await _next_host_continues(cluster, agent_id, script)
    assert not any("half a sentence" in str(m.content) for m in history)


async def test_an_idle_agent_is_restarted_without_a_note_and_stays_idle(
    cluster: Cluster, loguru_records: LogRecords
) -> None:
    agent_id, script = cluster.spawn(chat="hello", steps=[Step(text="hello back")])
    host = await cluster.host()
    await asyncio.wait_for(host.host.run_turn(agent_id), 60)
    assert script.calls == 1

    cluster.send(agent_id, "", kind="restart")
    await asyncio.wait_for(host.host.run_turn(agent_id), 60)

    assert [r.status for r in cluster.inbound(agent_id, "restart")] == ["done"]
    assert cluster.inbound(agent_id, "system_note") == []
    assert cluster.row(agent_id) == ("idling", None)
    messages = await host.messages(agent_id)
    assert sum(f"{_ACCEPTED} user" in str(m.content) for m in messages) == 1
    assert not any("while your turn was unfinished" in str(m.content) for m in messages)
    assert script.calls == 1
    assert not await cluster.woken_by_a_new_host(agent_id)
    await host.close()


# meiva:note
# A cancel halts a turn the user ended; the restart that follows must not resume it.
async def test_a_paused_agent_is_restarted_as_idle_not_as_an_interrupted_turn(
    cluster: Cluster,
) -> None:
    agent_id, script = cluster.spawn(chat="go", steps=[Step(holds=True)])
    host = await cluster.host()
    turn = host.start_turn(agent_id)
    await asyncio.wait_for(script.entered.wait(), 60)
    cluster.send(agent_id, "", kind="cancel")
    await asyncio.wait_for(turn, 60)
    assert cluster.inbound(agent_id, "cancel")[0].status == "done"

    cluster.send(agent_id, "", kind="restart")
    await asyncio.wait_for(host.host.run_turn(agent_id), 60)

    assert [r.status for r in cluster.inbound(agent_id, "restart")] == ["done"]
    assert cluster.inbound(agent_id, "system_note") == []
    messages = await host.messages(agent_id)
    assert sum(f"{_ACCEPTED} user" in str(m.content) for m in messages) == 1
    assert script.calls == 1
    await host.close()


async def test_a_restart_the_agent_asks_for_itself_resumes_the_turn_too(
    cluster: Cluster, tmp_path: pathlib.Path
) -> None:
    # meiva:note
    # `meiva.self.restart()` queues a `self` restart and raises `AgentRestart` in the exec
    # child; the child here raises it after the test has queued the row, as the SDK does.
    started, go, code = _waiting_code(
        tmp_path, then="from base.agents.lifecycle import AgentRestart\nraise AgentRestart()\n"
    )
    agent_id, script = cluster.spawn(chat="go", steps=[Step(code=code), Step(text="resumed")])
    host = await cluster.host()
    turn = host.start_turn(agent_id)
    await _until(started.exists)

    own = cluster.send(agent_id, "", kind="restart", source="self")
    # meiva:note
    # Long past its grace, a self restart is still not an interrupt: the code ends itself.
    cluster.let_grace_pass(own)
    await asyncio.sleep(INTERRUPT_POLL_S + 0.5)
    assert not turn.done()
    go.write_text("go")
    await asyncio.wait_for(turn, 60)

    [result] = _tool_results(await host.messages(agent_id))
    assert "ending" in result and "interrupted" not in result
    assert [r.status for r in cluster.inbound(agent_id, "restart")] == ["done"]
    assert cluster.row(agent_id) == ("idling", None)
    assert script.calls == 1
    [note] = _notes(cluster, agent_id)
    assert note.startswith(f"{_ACCEPTED} yourself while your turn was unfinished.")
    [queued] = cluster.inbound(agent_id, "system_note")
    assert queued.source == "self"
    await host.close()

    await _next_host_continues(cluster, agent_id, script)
