# pyright: reportOptionalSubscript=false
# Command.update is dict | None; tests always have an update field, narrowing is too verbose
# meiva:note
# Cancel and timeout paths of the exec node. Under the cycling topology a cancel goes to
# after_exec with halted=True, and claim then waits for the next inbound: the agent stands by.
# The durable interrupt case runs the real watcher against a pending inbound row; every other
# case runs a turn without an inbound queue (`ops_pool=None`), whose exec is uninterruptible,
# and scripts the child's outcome. Which outcome halts, what the envelope says and what is
# written back is `plan_dispatch`'s decision, tested without a node in `test_exec_plans.py`.

import asyncio
import threading
from typing import cast

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.execution.protocol import ResultPayload
from agent.graph.exec._result import LifecycleKind
from agent.graph.exec._stream import ExecOutputChunkPublisher
from agent.graph.exec.node import (
    ExecCancelled,
    ExecCrashed,
    ExecDone,
    ExecGoto,
    ExecLifecycle,
    ExecResult,
    ExecTimedOut,
    exec_node,
    exec_node_impl,
)
from agent.graph.exec.tests.cancel_fixture import ai_with_code
from agent.graph.exec.tests.process_inputs import ProcessCall, ScriptedExecProcess, exec_runtime
from agent.state import AgentState
from base.config.tests.configuration import defaults
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from base.log.tests.log_records import LogRecords
from base.native_process.stop_timing import RESTART_INTERRUPT_GRACE_S
from base.native_process.turn_identity import HostedTurnResources
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


# meiva:note
# A restart that has waited its grace ends the run, says so, and marks the turn unfinished so
# the claim that takes it resumes the turn instead of idling the agent.
@pytest.mark.parametrize(
    ("kind", "source", "waited_s", "marker", "unfinished"),
    [
        ("cancel", "user", 0, "[cancelled by user]", False),
        ("cancel", "agent:9", 0, "[cancelled by system]", False),
        ("restart", "user", RESTART_INTERRUPT_GRACE_S + 1, "[interrupted by restart]", True),
    ],
)
async def test_exec_node_preserves_durable_interrupt_attribution(
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    kind: str,
    source: str,
    waited_s: float,
    marker: str,
    unfinished: bool,
    database_gate: ProcessDbGate,
) -> None:
    """The real watcher aborts once, preserves partial output, and leaves claim its command."""
    agent_id = create_agent(db_conn)
    command = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,created_at) "
        "VALUES(%s,%s,%s,'',clock_timestamp() - make_interval(secs => %s)) RETURNING id",
        (agent_id, kind, source, waited_s),
    ).fetchone()
    assert command is not None
    db_conn.commit()

    class InterruptedProcess(ScriptedExecProcess):
        async def answer(
            self, call: ProcessCall, outcome: tuple[ExecResult, ResultPayload | None]
        ) -> tuple[ExecResult, ResultPayload | None]:
            await asyncio.wait_for(call.cancel_event.wait(), timeout=5)
            return outcome

    process = InterruptedProcess(
        [(ExecCancelled(output="external side effect already happened\n"), None)]
    )

    result = await exec_node(
        AgentState(messages=[ai_with_code("pass")], halted=False),
        exec_runtime(
            hosted_resources=hosted_resources, ops_pool=aops_pool, database_gate=database_gate
        ),
        {"configurable": {"thread_id": str(agent_id)}},
        process=process,
    )
    assert result.goto == "after_exec"
    assert result.update["halted"] is True
    message = cast(ToolMessage, result.update["messages"][0])
    assert marker in message.content
    assert cast(dict[str, object], result.update).get("interrupted_by_restart", False) is unfinished
    assert (kind == "restart") == ("cancelled by" not in message.content)
    assert "external side effect already happened" in message.content
    assert db_conn.execute(
        "SELECT status,claimed_at FROM inbound_messages WHERE id=%s",
        (command[0],),
    ).fetchone() == ("pending", None)


def _tool_content(command: Command[ExecGoto]) -> str:
    update = cast(dict[str, list[ToolMessage]], command.update)
    return str(update["messages"][0].content)


async def test_exec_node_cancel_event_returns_cancelled_command(
    database_gate: ProcessDbGate, live_events: LiveEvents
) -> None:
    process = ScriptedExecProcess([(ExecCancelled(output="partial work\n"), None)])
    state = AgentState(messages=[ai_with_code('print("x")')], halted=False)
    runtime = exec_runtime(event_publisher=live_events.publisher, database_gate=database_gate)

    result = await exec_node(state, runtime, _CONFIG, process=process)

    assert result.goto == "after_exec"
    content = _tool_content(result)
    assert "Code execution output" in content
    assert "[cancelled by user]" in content
    assert "partial work" in content
    assert cast(dict[str, object], result.update)["halted"] is True
    roles = [event["role"] for event in await live_events.events()]
    assert [role for role in roles if role in {"exec_start", "cancelled", "exec_output"}] == [
        "exec_start",
        "cancelled",
        "exec_output",
    ]


async def test_exec_node_completes_normally_when_nothing_cancels(
    database_gate: ProcessDbGate,
) -> None:
    process = ScriptedExecProcess([(ExecDone(output="hello\n"), None)])
    state = AgentState(messages=[ai_with_code('print("hello")')], halted=False)

    result = await exec_node(
        state, exec_runtime(database_gate=database_gate), _CONFIG, process=process
    )

    content = _tool_content(result)
    assert "Code execution output" in content
    assert "hello" in content
    assert "[exit" not in content


async def test_exec_node_timeout_returns_a_marker_and_does_not_halt(
    database_gate: ProcessDbGate,
) -> None:
    process = ScriptedExecProcess([(ExecTimedOut(output="partial work\n"), None)])
    state = AgentState(messages=[ai_with_code('print("long task")')], halted=False)

    result = await exec_node(
        state, exec_runtime(database_gate=database_gate), _CONFIG, process=process
    )

    assert result.goto == "after_exec"
    content = _tool_content(result)
    assert "Code execution output" in content
    assert "[timeout after 60s]" in content
    assert "[cancelled by user]" not in content
    assert cast(dict[str, object], result.update)["halted"] is False


async def test_exec_node_timeout_empty_output(database_gate: ProcessDbGate) -> None:
    process = ScriptedExecProcess([(ExecTimedOut(output=""), None)])
    state = AgentState(messages=[ai_with_code('print("x")')], halted=False)

    result = await exec_node(
        state, exec_runtime(database_gate=database_gate), _CONFIG, process=process
    )

    content = _tool_content(result)
    assert "[timeout after 60s]" in content
    assert "(no output)" in content


def test_streaming_textio_concurrent_writes() -> None:
    from agent.graph.exec._stream import StreamingTextIO

    stream = StreamingTextIO(max_chars=1_000_000)
    n_writers = 4
    writes_per = 200
    payload = "x" * 100

    def writer() -> None:
        for _ in range(writes_per):
            stream.write(payload)

    threads = [threading.Thread(target=writer) for _ in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    full = stream.getvalue()
    assert len(full) == n_writers * writes_per * len(payload)
    assert all(c == "x" for c in full)


def test_streaming_textio_take_pending_increments() -> None:
    from agent.graph.exec._stream import StreamingTextIO

    stream = StreamingTextIO(max_chars=1_000_000)
    stream.write("aaa")
    assert stream.take_pending() == "aaa"
    assert stream.take_pending() == ""
    stream.write("bbb")
    stream.write("ccc")
    assert stream.take_pending() == "bbbccc"
    assert stream.getvalue() == "aaabbbccc"


async def test_chunk_publisher_uses_correct_item_id(live_events: LiveEvents) -> None:
    pub = ExecOutputChunkPublisher(live_events.publisher, agent_id=42, item_id="7.0")

    pub.publish("hello")
    pub.keepalive()
    pub.publish(" world")

    events = await live_events.events()
    assert [(event["content"], event["keepalive"]) for event in events] == [
        ("hello", False),
        ("", True),
        (" world", False),
    ]
    assert all(event["agent_id"] == 42 and event["item_id"] == "7.0" for event in events)


async def test_exec_node_compaction_writes_nothing_back(
    database_gate: ProcessDbGate, live_events: LiveEvents
) -> None:
    process = ScriptedExecProcess(
        [(ExecLifecycle(output="user prep work\n", kind=LifecycleKind.SYSTEM_HALT), None)]
    )
    state = AgentState(messages=[ai_with_code('meiva.self.compact("s")')], halted=False)
    runtime = exec_runtime(event_publisher=live_events.publisher, database_gate=database_gate)

    result = await exec_node(state, runtime, _CONFIG, process=process)

    update = cast(dict[str, object], result.update)
    assert update["halted"] is True
    assert update["messages"] == []
    roles = [event["role"] for event in await live_events.events()]
    assert "exec_start" in roles
    assert "exec_output" not in roles


@pytest.mark.parametrize("kind", [LifecycleKind.TERMINATION, LifecycleKind.RESTART])
async def test_exec_node_terminate_and_restart_halt_without_a_marker(
    kind: LifecycleKind, database_gate: ProcessDbGate
) -> None:
    process = ScriptedExecProcess([(ExecLifecycle(output="", kind=kind), None)])
    state = AgentState(messages=[ai_with_code("meiva.self.terminate()")], halted=False)

    result = await exec_node(
        state, exec_runtime(database_gate=database_gate), _CONFIG, process=process
    )

    assert cast(dict[str, object], result.update)["halted"] is True
    content = _tool_content(result)
    assert "[cancelled by user]" not in content
    assert "[timeout after 60s]" not in content
    assert "[system halt]" not in content


async def test_exec_node_ordinary_exception_logs_exec_failed_at_info(
    loguru_records: LogRecords, database_gate: ProcessDbGate
) -> None:
    process = ScriptedExecProcess(
        [
            (
                ExecCrashed(output="Traceback...\nValueError: boom\n", exc=ValueError("boom")),
                None,
            )
        ]
    )
    state = AgentState(messages=[ai_with_code('raise ValueError("boom")')], halted=False)

    result = await exec_node(
        state, exec_runtime(database_gate=database_gate), _CONFIG, process=process
    )

    assert cast(dict[str, object], result.update)["halted"] is False
    failed = [r for r in loguru_records if r["extra"].get("event") == "exec_failed"]
    assert len(failed) == 1
    assert failed[0]["level"].name == "INFO"
    assert "ValueError" in failed[0]["message"]


_TOOL_CALL_AIMESSAGE = AIMessage(
    content="",
    tool_calls=[
        {"name": "execute_code", "args": {"code": "x = 1"}, "id": "tc-1", "type": "tool_call"}
    ],
    response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
)


async def test_exec_node_shield_turns_a_hung_process_into_a_timeout_result(
    database_gate: ProcessDbGate,
) -> None:
    class HangingProcess(ScriptedExecProcess):
        async def answer(
            self, call: ProcessCall, outcome: tuple[ExecResult, ResultPayload | None]
        ) -> tuple[ExecResult, ResultPayload | None]:
            await asyncio.Future[None]()
            raise AssertionError("unreachable")

    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)
    runtime = exec_runtime(
        agent=AgentSlices.resolve(default_reader=defaults(sandbox__exec_node_timeout_seconds=0.05)),
        database_gate=database_gate,
    )

    result = await exec_node_impl(
        state, runtime, _CONFIG, process=HangingProcess([(ExecDone(output=""), None)])
    )

    update = cast(dict[str, list[ToolMessage]], result.update)
    assert "timeout" in str(update["messages"][-1].content).lower()


async def test_exec_node_shield_does_not_fire_when_the_process_is_fast(
    database_gate: ProcessDbGate,
) -> None:
    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)
    runtime = exec_runtime(
        agent=AgentSlices.resolve(
            default_reader=defaults(
                sandbox__exec_node_timeout_seconds=10.0, sandbox__exec_timeout_seconds=30.0
            )
        ),
        database_gate=database_gate,
    )

    result = await exec_node_impl(
        state,
        runtime,
        _CONFIG,
        process=ScriptedExecProcess([(ExecDone(output="hello"), None)]),
    )

    update = cast(dict[str, list[ToolMessage]], result.update)
    assert "hello" in str(update["messages"][-1].content)
