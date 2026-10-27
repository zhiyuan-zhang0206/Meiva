# pyright: reportOptionalSubscript=false
# Command.update type dict | None; tests always have update field, narrowing too verbose
"""agent/graph.py streaming behavior tests (async).

mock `BaseChatModel.astream` returns async chunk iterator + AsyncMock redis, validate:
  - llm_node first publishes a code_start
  - each chunk publishes a code_delta (content = chunk.content)
  - llm_node returns AIMessage.content = all chunks concatenated
  - exec_node publishes exec_start at the start
  - exec_node publishes output after finishing

Assert using `EVENT_ADAPTER.validate_json` deserialization——same code path as UI tailer,
if any side field changes, the other side test will turn red."""

import random
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.graph import LlmLedger, exec_node, llm_node
from agent.graph.exec.output import overflow_dir
from agent.graph.exec.process import SubprocessExecProcess
from agent.graph.exec.tests.process_inputs import live_parent_facts
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.tests.units import spawn_agent
from base.clock import Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import ExecOutput, ExecStart
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home, resolve_meiva_home
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources
from meiva.tests.fixtures.pin_agent import snapshot_clients


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


# meiva:note
# Assembles the Runtime a node runs in. Without an `ops_pool` the turn has no inbound queue (the
# container/eval shape), so nothing can interrupt it and it touches no database; a test that reads
# the database passes the throwaway pool and the real agent id. SSE fan-out goes through
# `ctx.event_publisher.emit`; the default is a sink so the node's `assert ctx.event_publisher`
# passes, and a test that reads the events passes `live_events.publisher`.
def _make_runtime(
    hosted_resources: HostedTurnResources,
    *,
    llm=None,
    event_publisher=None,
    database_gate: ProcessDbGate,
    ops_pool: AsyncConnectionPool | None = None,
    agent_id: int = 7,
    config_defaults: Mapping[str, Any] | None = None,
) -> Runtime[MeivaContext]:
    if llm is None:
        llm = MagicMock()
    if isinstance(llm, MagicMock):
        llm.bind_tools.return_value = llm
    ctx = MeivaContext(
        meiva_home=resolve_meiva_home(),
        hosted_resources=hosted_resources,
        ops_pool=ops_pool,
        llm=llm,  # pyright: ignore[reportUnknownArgumentType]
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),  # pyright: ignore[reportUnknownArgumentType]
        agent=AgentSlices.resolve(default_reader=defaults(**(config_defaults or {}))),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(rng=seeded_rng()),
        clients=snapshot_clients(),
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
        catalog=build_model_catalog(home=meiva_home()),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def _ai_with_code(code: str) -> AIMessage:
    """Single tool reconstructed wire format: AIMessage with execute_code tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "call_test"}],
    )


async def _aiter(chunks: list[AIMessageChunk]) -> AsyncIterator[AIMessageChunk]:
    """Helper: wrap list into async iterator -- assign fake_llm.astream.return_value
    this generator so `async for` can consume. Each test creates a new generator,
    not reusing across tests (generators are single-use)."""
    for c in chunks:
        yield c


async def test_llm_node_collects_chunks_into_final_message(
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
) -> None:
    """llm_node merges streaming chunks into AIMessage into state.messages.

    streaming → Redis behavior is separately tested in agent/graph/tests/test_callbacks.py
    via RedisStreamHandler (agent/graph/_callbacks.py) inline dispatch inside _stream loop;
    this test only verifies node's chunk-to-AIMessage merging semantics.
    """
    fake_llm = MagicMock()
    # Last chunk carries usage_metadata——real provider stream tail has this shape,
    # llm_node internally assert final_msg.usage_metadata must exist (fail-fast guard new generated
    # AIMessage metadata not empty).
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content="hello "),
            AIMessageChunk(content="from "),
            AIMessageChunk(
                content="agent",
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    state = AgentState(messages=[HumanMessage(content="hello")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    result = await llm_node(
        state,
        _make_runtime(hosted_resources=hosted_resources, llm=fake_llm, database_gate=database_gate),
        config,
        ledger=LlmLedger(),
        rng=_rng(),
    )

    assert isinstance(result, Command)
    assert result.update["messages"][0].content == "hello from agent"  # pyright: ignore[reportUnknownMemberType]


# meiva:note
# (last_active_at set, last_message_text, corpse marker still set) of the agent's row.
def _turn_stamps(db: psycopg.Connection, agent_id: int) -> tuple[bool, str | None, bool]:
    row = db.execute(
        "SELECT last_active_at IS NOT NULL, last_message_text, last_turn_fatal_at IS NOT NULL "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row


def _mark_corpse(db: psycopg.Connection, agent_id: int) -> None:
    db.execute("UPDATE agents_meta SET last_turn_fatal_at = now() WHERE id = %s", (agent_id,))
    db.commit()


async def test_llm_node_stamps_last_active_at_with_text(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    """A completed turn that produced text writes both last_active_at (the
    heartbeat idle clock's real-activity anchor) and last_message_text in one
    UPDATE — so a genuine turn resets the agent's idle timer."""
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _mark_corpse(db_conn, agent_id)
    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content="hello "),
            AIMessageChunk(
                content="world",
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    runtime = _make_runtime(
        hosted_resources,
        llm=fake_llm,
        database_gate=database_gate,
        ops_pool=aops_pool,
        agent_id=agent_id,
    )
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    await llm_node(
        state,
        runtime,
        {"configurable": {"thread_id": str(agent_id)}},
        ledger=LlmLedger(),
        rng=_rng(),
    )

    active, text, corpse = _turn_stamps(db_conn, agent_id)
    assert active, "a completed turn stamps last_active_at"
    assert text == "hello world", "a turn with text also persists last_message_text"
    assert not corpse, (
        "a completed turn clears the corpse marker (first success closes the breaker)"
    )


async def test_llm_node_stamps_last_active_at_on_tool_only_turn(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    """A tool-only turn (code, no text) is still real work: it writes
    last_active_at (without last_message_text, since there is no AI text)."""
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _mark_corpse(db_conn, agent_id)
    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content=""),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "execute_code", "args": '{"code":"x"}', "id": "call_1", "index": 0},
                ],
            ),
            AIMessageChunk(
                content="",
                response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    runtime = _make_runtime(
        hosted_resources,
        llm=fake_llm,
        database_gate=database_gate,
        ops_pool=aops_pool,
        agent_id=agent_id,
    )
    state = AgentState(messages=[HumanMessage(content="run it")], halted=False)
    await llm_node(
        state,
        runtime,
        {"configurable": {"thread_id": str(agent_id)}},
        ledger=LlmLedger(),
        rng=_rng(),
    )

    active, text, corpse = _turn_stamps(db_conn, agent_id)
    assert active, "a tool-only turn must still stamp last_active_at"
    assert text is None, "no AI text → no last_message_text"
    assert not corpse, "a tool-only turn also clears the corpse marker"


async def test_llm_node_dispatches_chunks_to_handler_with_anthropic_shape(
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
) -> None:
    """ChatAnthropic + bind_tools real chunk shape (content is list-of-blocks,
    not string) goes through _stream loop → handler.process_chunk → publish full event stream.

    Regression guard: early llm_node went through LangChain `on_llm_new_token` callback,
    after switching to ChatAnthropic callback never fires (`isinstance(content, str)` always
    False, see langchain_anthropic/chat_models.py:1267) — entire stream publish
    path broken. This test uses real anthropic chunk shape to guard inline dispatch path.
    """
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _aiter(
        [
            # message_start: empty content
            AIMessageChunk(content=[]),
            # thinking_delta
            AIMessageChunk(content=[{"type": "thinking", "thinking": "planning", "index": 0}]),
            # text_delta
            AIMessageChunk(content=[{"type": "text", "text": "hi", "index": 1}]),
            # tool_use start: content=[tool_use block] + tool_call_chunks=[args=""]
            AIMessageChunk(
                content=[
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "execute_code",
                        "input": {},
                        "index": 2,
                    }
                ],
                tool_call_chunks=[
                    {"name": "execute_code", "args": "", "id": "call_1", "index": 2},
                ],
            ),
            # input_json_delta: tool_call_chunks carry partial JSON delta
            AIMessageChunk(
                content=[],
                tool_call_chunks=[
                    {"name": None, "args": '{"code":"x"}', "id": None, "index": 2},
                ],
            ),
            # tail message_delta with usage info
            AIMessageChunk(
                content=[],
                response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    await llm_node(
        state,
        _make_runtime(
            hosted_resources=hosted_resources,
            llm=fake_llm,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
        ),
        config,
        ledger=LlmLedger(),
        rng=_rng(),
    )

    roles = [e.role for e in await live_events.typed()]
    # Three independent streams all trigger *Start, finally LLMDone sent by finish()
    assert "reasoning_start" in roles
    assert "reasoning_delta" in roles
    assert "chat_start" in roles
    assert "chat_delta" in roles
    assert "code_start" in roles
    assert "code_delta" in roles
    # node_lifecycle enter publishes timeline_snapshot first (before the LLM
    # stream starts). finish() emits LLMDone when the stream loop completes;
    # TokenUsage is published after final_msg processing, so it lands last.
    assert roles[0] == "timeline_snapshot"
    assert roles[-2:] == ["llm_done", "token_usage"]


async def test_llm_node_publishes_reasoning_tokens(
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
) -> None:
    """usage_metadata.output_token_details.reasoning rides through to the
    published TokenUsage event end-to-end (gemini/openai reasoning-token
    surfacing — not just the model/UI layers). Drives the real _llm_node_impl
    publish path with a gemini-shaped final chunk."""
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content=[{"type": "text", "text": "ok", "index": 0}]),
            AIMessageChunk(
                content=[],
                response_metadata={"model_provider": "google_genai", "finish_reason": "STOP"},
                usage_metadata={
                    "input_tokens": 5,
                    "output_tokens": 200,
                    "total_tokens": 205,
                    "output_token_details": {"reasoning": 180},
                },
            ),
        ]
    )
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    await llm_node(
        state,
        _make_runtime(
            hosted_resources=hosted_resources,
            llm=fake_llm,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
        ),
        config,
        ledger=LlmLedger(),
        rng=_rng(),
    )

    usage = [e for e in await live_events.typed() if e.role == "token_usage"]
    assert len(usage) == 1
    assert usage[0].reasoning_tokens == 180
    assert usage[0].output_tokens == 200


async def test_llm_node_preserves_usage_metadata(
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
) -> None:
    """usage_metadata must be on the final AIMessage——evals / cost tracking all rely on this
    field. If someday llm_node falls back to only concatenating content without accumulating AIMessageChunk, this
    test will turn red."""
    # Last chunk carries usage_metadata——real Anthropic stream has this
    # shape (usage in message_delta event, lands on last chunk)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content="print(1)"),
            AIMessageChunk(
                content="",
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata={
                    "input_tokens": 42,
                    "output_tokens": 7,
                    "total_tokens": 49,
                },
            ),
        ]
    )
    state = AgentState(messages=[HumanMessage(content="go")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    result = await llm_node(
        state,
        _make_runtime(hosted_resources=hosted_resources, llm=fake_llm, database_gate=database_gate),
        config,
        ledger=LlmLedger(),
        rng=_rng(),
    )

    assert isinstance(result, Command)
    msg = result.update["messages"][0]
    assert msg.content == "print(1)"  # pyright: ignore[reportUnknownMemberType]
    assert msg.usage_metadata == {"input_tokens": 42, "output_tokens": 7, "total_tokens": 49}  # pyright: ignore[reportUnknownMemberType]


async def test_exec_node_publishes_exec_start_and_output(
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
) -> None:
    """exec_node's two events:
    - before executing publish `exec_start` (UI draws [executing] marker)
    - after finishing publish `exec_output` (UI dev mode sees agent-visible stdout)
    """
    state = AgentState(messages=[_ai_with_code('print("ok")')], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    # exec_node actually runs subprocess (async) -- use simplest code to avoid side effects
    await exec_node(
        state,
        _make_runtime(
            hosted_resources=hosted_resources,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
        ),
        config,
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    events = await live_events.typed()
    exec_starts = [e for e in events if isinstance(e, ExecStart)]
    # item_id = f"{exec_msg_idx}.0" where exec_msg_idx = len(state.messages) = 1
    assert exec_starts == [ExecStart(agent_id=7, item_id="1.0")]

    exec_outputs = [e for e in events if isinstance(e, ExecOutput)]
    assert len(exec_outputs) == 1
    assert exec_outputs[0].agent_id == 7
    assert "ok" in exec_outputs[0].content


async def test_exec_node_output_uses_wrap_code_output_envelope(
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
) -> None:
    """exec_node after normal completion, appended HumanMessage uses wrap_code_output
    envelope('Code execution output:' prefix), no longer old '[exec output]\\n...'
    format; envelope without [exit N] line(exit code goes through ToolMessage metadata)."""
    # print + flush ensures stdout has content
    state = AgentState(
        messages=[_ai_with_code("import sys; print('envelope_test'); sys.stdout.flush()")],
        halted=False,
    )
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    result = await exec_node(
        state,
        _make_runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        config,
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    assert isinstance(result, Command)
    msg = result.update["messages"][0]
    content = msg.content  # pyright: ignore[reportUnknownMemberType]
    # New format
    assert content.startswith("Code execution output"), (  # pyright: ignore[reportUnknownMemberType]
        f"expected wrap_code_output format, got: {content[:60]!r}"
    )
    assert "envelope_test" in content
    assert "[exit" not in content
    # exec wall-clock captured (drives the 'ran in Xs' chip); a real run is
    # non-negative ms — just assert the contract that the field is populated.
    assert isinstance(msg.additional_kwargs["meiva_exec_ms"], int)  # pyright: ignore[reportUnknownMemberType]
    assert msg.additional_kwargs["meiva_exec_ms"] >= 0  # pyright: ignore[reportUnknownMemberType]
    # Old format should not appear
    assert not content.startswith("[exec output]")  # pyright: ignore[reportUnknownMemberType]


async def test_exec_node_protects_archives_referenced_by_its_current_state(
    unit_home: Path,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
) -> None:
    """A real child exec cannot evict the previous output still in native context."""
    # Bodies exceed the 300-line soft-crop trigger; the new body stays under
    # exec_output_max_chars so a reference-protected skip leaves it fully
    # inline (6 x "old payload " = 72 chars + newline -> 340 lines, 24,820 chars).
    old_body = ("old payload " * 6 + "\n") * 340
    new_body = ("new payload " * 6 + "\n") * 340
    runtime = _make_runtime(
        hosted_resources,
        database_gate=database_gate,
        config_defaults={"sandbox__exec_output_crop_archive_max_bytes": len(old_body)},
    )
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    first = await exec_node(
        AgentState(
            messages=[_ai_with_code("print(('old payload ' * 6 + '\\n') * 340, end='')")],
            halted=False,
        ),
        runtime,
        config,
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )
    prior_output = first.update["messages"][0]
    directory = overflow_dir(7, home=resolve_meiva_home())
    archive = next(directory.glob("crop_*.txt"))
    state = AgentState(
        messages=[
            prior_output,
            _ai_with_code("print(('new payload ' * 6 + '\\n') * 340, end='')"),
        ],
        halted=False,
    )
    result = await exec_node(
        state, runtime, config, process=SubprocessExecProcess(live_parent_facts(), rng=_rng())
    )

    assert new_body in result.update["messages"][0].content  # pyright: ignore[reportUnknownMemberType]
    assert archive.read_text() == old_body
    assert list(directory.glob("crop_*.txt")) == [archive]


# Removed test_exec_node_cancel_output_uses_wrap_code_output_cancelled——it used
# mock _run_in_subprocess synchronously throw SubprocessCancelledPartial simulating cancel.
# After Step 2 cancel-in-node RAII exec_node no longer catches top-level
# SubprocessCancelledPartial (task.cancel() path deleted); real cancel goes through
# cancel_event race → _cancel_subprocess_task internal catch. Coverage path see
# agent/graph/exec/tests/test_cancel.py::test_exec_node_cancel_event_race_captures_partial_stdout
