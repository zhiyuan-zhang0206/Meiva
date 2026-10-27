# pyright: reportOptionalSubscript=false
"""Mutmut gap tests for `agent/graph/llm/node.py` (baseline PR #302).

`capture_meiva_overview` in `_base_prompt.py` captures `meiva.help(meiva)` stdout.
The cancel-task/stream-first boundary discards partial generations and publishes
Cancelled only for cancellation; normal completion calls handler.finish().
Stop-reason and thinking-block validation cover the remaining mutation gaps.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Mapping
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx2
import openai
import psycopg
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import ExecutionInfo, Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.graph import llm_node
from agent.graph.interrupt.tests.queued_commands import queue_command
from agent.graph.llm_errors import LlmLedger
from agent.graph.prompt.system_prompt import capture_meiva_overview, get_meiva_overview
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.clock import Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config import settings
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import Cancelled
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices, LlmCallPolicy
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


def _config(agent_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(agent_id)}}


@pytest.fixture
def ledger() -> LlmLedger:
    """A fresh ledger per test: the node's error counts and silent-idle budget start empty."""
    return LlmLedger()


# ───────────── capture_meiva_overview ─────────────


def test_capture_meiva_overview_returns_non_empty_string() -> None:
    """Direct import + call of `capture_meiva_overview` — lock that `buf = io.StringIO()`
    was not changed to None or list(); `redirect_stdout(buf)` actually captures the
    output of `meiva.help(meiva)` (mutation `redirect_stdout(None)` would let stdout leak → return ''),
    `buf.getvalue()` is not empty."""
    overview = capture_meiva_overview()

    assert isinstance(overview, str)
    assert len(overview) > 0, "buf did not capture meiva.help stdout — redirect_stdout was severed"


def test_capture_meiva_overview_surfaces_public_sdk_index() -> None:
    # The overview = `# meiva` H1 + one entry per public top-level namespace as
    # `from . import X` + that module's docstring. This is the SDK index, so the
    # agent discovers the gateway primitives (agents / watcher / self) without
    # spelunking; full per-namespace detail stays on demand via meiva.help(meiva.X).
    # The root docstring was deliberately removed (sysprompt verbosity audit,
    # PR #840) — the `# meiva` heading stands alone.
    overview = capture_meiva_overview()

    assert overview.startswith("# meiva\n\n"), (
        f"overview does not start with `# meiva` heading: {overview[:200]!r}"
    )
    assert "Drill into" not in overview, (
        f"removed root docstring still rendered: {overview[:200]!r}"
    )
    # gateway primitives surfaced as index entries
    for name in ("agents", "watcher", "self", "shell"):
        assert f"from . import {name}" in overview, (
            f"surface primitive {name!r} should appear in overview: {overview[:600]!r}"
        )
    # only `# meiva` heading; children render as `from . import` import stubs, not headings
    heading_lines = [
        line
        for line in overview.splitlines()
        if line.lstrip().startswith("#") and not line.lstrip().startswith("#!")
    ]
    assert heading_lines == ["# meiva"], (
        f"overview should have only one `# meiva` heading, got: {heading_lines!r}"
    )


def test_capture_meiva_overview_is_pure_no_stdout_leak(capsys) -> None:
    """`redirect_stdout(buf)` must capture all meiva.help output into buf, **not**
    leak to main stdout. Lock that the line `with contextlib.redirect_stdout(buf):`
    was not changed to `with contextlib.redirect_stdout(sys.stdout):` — such a mutation
    would make meiva.help actually print to stdout, then capsys.readouterr().out would not be empty."""
    capsys.readouterr()  # clear any prior output  # pyright: ignore[reportUnknownMemberType]
    overview = capture_meiva_overview()
    captured = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]

    assert overview, "overview is empty"
    assert captured.out == "", (  # pyright: ignore[reportUnknownMemberType]
        f"redirect_stdout ineffective, meiva.help content leaked to main stdout: {captured.out[:200]!r}"  # pyright: ignore[reportUnknownMemberType]
    )


def test_get_meiva_overview_advertises_installed_plugin_namespace() -> None:
    """A plugin-installed namespace **does** appear in the overview index.

    The namespace itself must be discoverable at the top level even if the
    plugin adds no declared system prompt section of its own — otherwise a
    top-level namespace would silently vanish and the agent couldn't find it.
    The plugin promotes its *members* in a section; the overview lists the
    *namespace*.
    """
    from types import SimpleNamespace

    from meiva.sdk_surface import plugins

    fake_ns = SimpleNamespace(
        __doc__="Fake plugin namespace just for this test.",
        __all_for_meiva__=["ping"],
        ping=lambda: "pong",
    )
    fake_ns.ping.__doc__ = "Return pong."

    added = plugins.install_namespace("fake-plugin", "fake_late", fake_ns, {})
    try:
        overview = get_meiva_overview()
        assert "fake_late" in overview, (
            f"overview should advertise the installed plugin namespace (otherwise a namespace "
            f"without a section would disappear). overview:\n{overview[:600]}"
        )
    finally:
        plugins.uninstall(added)


# meiva:note
# Cancel-detection boundary (mutmut_44: `if cancel_task in done:` inverted to `not in done`).
# The race tests do not assert the exact contents of the done set, so the inverted test still
# passed; the cases below tell the two path invariants apart, "cancel first in done" from "stream
# first in done", with a real queued cancel command and the events a subscriber receives.


# meiva:note
# Assembles the Runtime a node runs in. Without an `ops_pool` the turn has no inbound queue (the
# container/eval shape), so nothing can interrupt it and it touches no database; a test that
# needs an interrupt or reads the database passes the throwaway pool. SSE fan-out goes through
# `ctx.event_publisher.emit`; the default is a sink so the node's `assert ctx.event_publisher`
# passes, and a test that reads the events passes `live_events.publisher`.
def make_runtime(
    *,
    resources: HostedTurnResources,
    llm=None,
    event_publisher=None,
    execution_info: ExecutionInfo | None = None,
    database_gate: ProcessDbGate,
    pins: Mapping[str, Any] | None = None,
    ops_pool: AsyncConnectionPool | None = None,
) -> Runtime[MeivaContext]:
    if llm is None:
        llm = MagicMock()
    if isinstance(llm, MagicMock):
        llm.bind_tools.return_value = llm
    ctx = MeivaContext(
        hosted_resources=resources,
        ops_pool=ops_pool,
        llm=llm,  # pyright: ignore[reportUnknownArgumentType]
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),  # pyright: ignore[reportUnknownArgumentType]
        agent=AgentSlices.resolve(pins, default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(rng=seeded_rng()),
        catalog=build_model_catalog(home=meiva_home()),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx, execution_info=execution_info)


# meiva:note
# Whether the published events contain a Cancelled(agent_id=...).
def _has_cancelled_event(events: list[Any], agent_id: int) -> bool:
    return any(isinstance(e, Cancelled) and e.agent_id == agent_id for e in events)


async def test_cancel_branch_publishes_cancelled_and_returns_halted(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
) -> None:
    """cancel_event arrives in done set first → llm_node must enter the cancel branch:
    (a) publish a Cancelled event to settings.data_plane.events_channel
    (b) goto=after_exec + halted=True
    (c) the cancel path does **not** call handler.finish() (no LLMDone/TokenUsage publish)

    Locks mutmut_44 `if cancel_task in done:` inversion: after inversion, cancel_task is in
    done but condition is False, falling into stream-normal branch, running stream_task.result()
    triggers CancelledError and raises out the whole node, resulting in no Cancelled event and
    no halted Command — this test verifies both the Cancelled event and the Command shape.
    """

    async def _stream_then_hang() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="partial")
        await asyncio.Future()  # hang forever

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()
    agent_id = create_agent(db_conn)
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    async def _trigger():
        await asyncio.sleep(0.1)
        queue_command(db_conn, agent_id, "cancel")

    trigger = asyncio.create_task(_trigger())
    result = await llm_node(
        state,
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
            ops_pool=aops_pool,
        ),
        _config(agent_id),
        ledger=ledger,
        rng=_rng(),
    )
    await trigger
    events = await live_events.typed()

    # (a) Cancelled event emitted (cancel branch-exclusive side effect)
    assert _has_cancelled_event(events, agent_id), (
        "cancel branch did not emit Cancelled event —— `cancel_task in done` branch not taken"
    )
    # (b) Command shape: goto=after_exec + halted=True
    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is True
    # (c) cancel path does not call handler.finish() —— no llm_done / token_usage emit
    roles = [e.role for e in events]
    assert "llm_done" not in roles, (
        "cancel branch must not emit LLMDone (handler.finish() must not be called)"
    )
    assert "token_usage" not in roles, (
        "cancel branch must not emit TokenUsage (took normal-completion path)"
    )


async def test_stream_normal_completion_no_cancelled_event(
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
) -> None:
    """stream completes first (cancel_event never set) → takes stream-normal branch:
    (a) **does not** publish Cancelled event
    (b) goto=before_exec (tool_calls present → go to exec) or after_exec (idle)
    (c) handler.finish() called → LLMDone event present

    Locks mutmut_44 `if cancel_task in done:` inversion: after inversion the stream-normal
    branch is mistakenly taken as cancel-branch, would publish Cancelled event in a non-cancel
    scenario.
    """

    async def _fast_complete() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="hi",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _fast_complete()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    # meiva:note
    # No interrupt is queued, so the stream completes naturally.
    result = await llm_node(
        state,
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
        ),
        CONFIG,
        ledger=ledger,
        rng=_rng(),
    )
    events = await live_events.typed()

    # (a) no Cancelled event
    assert not _has_cancelled_event(events, agent_id=7), (
        "stream-normal branch mistakenly took cancel branch —— `cancel_task in done` mutation flipped"
    )
    # (b) Command shape: end_turn + no tool_calls → halted=True goto=after_exec
    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is True
    # (c) finish() called → LLMDone emitted
    assert "llm_done" in [e.role for e in events], (
        "stream-normal branch must call handler.finish() and emit LLMDone"
    )


async def test_silent_idle_with_reasoning_continue_loops_not_raises(
    hosted_resources: HostedTurnResources, ledger: LlmLedger, database_gate: ProcessDbGate
) -> None:
    """No tool_call AND empty text BUT output_tokens > 0 (model produced
    reasoning) → the node no longer raises. It commits the reasoning AIMessage
    and returns halted=False so the claim node loops straight back to the LLM
    (the meiva_silent_idle plugin then injects a Continue nudge). No token-wasting
    blind re-stream."""

    async def _empty_complete() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _empty_complete()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state,
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=MagicMock(),
            database_gate=database_gate,
        ),
        CONFIG,
        ledger=ledger,
        rng=_rng(),
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    # continue-loop: halted=False so claim returns to before_llm without idling
    assert result.update["halted"] is False
    # the reasoning AIMessage is preserved in state.messages
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)


async def test_truly_empty_no_reasoning_halts_with_warning(
    hosted_resources: HostedTurnResources,
    loguru_records,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
) -> None:
    """No tool_call AND empty text AND output_tokens=0 (model truly produced
    nothing, not even reasoning) → the existing WARNING + halt path still
    applies — retrying a deterministic empty output wastes API credits."""

    async def _empty_complete() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _empty_complete()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state,
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=MagicMock(),
            database_gate=database_gate,
        ),
        CONFIG,
        ledger=ledger,
        rng=_rng(),
    )

    assert isinstance(result, Command)
    assert result.update["halted"] is True
    silent = [
        r
        for r in loguru_records
        if "EMPTY text" in r["message"] and r["level"].name == "WARNING"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert len(silent) == 1, "truly empty (no tokens) must log exactly one distinct WARNING"  # pyright: ignore[reportUnknownArgumentType]


# ───────────── extra actionable mutation kill ─────────────


def test_validate_stop_reason_unexpected_carries_stop_reason_and_output_tokens() -> None:
    """`LLMStreamUnexpectedStopReasonError` raise must carry stop_reason and
    output_tokens attributes (programmatic dispatch does not rely on regex parsing the message).
    mutmut: `stop_reason=stop_reason` → `stop_reason=None` /
    `output_tokens=output_tokens` → `output_tokens=None`.

    Existing `test_validate_raises_on_pause_turn` only matches the message and does not read
    attributes; `test_validate_raises_on_max_tokens` reads attributes but only covers the
    Truncated subclass path — the unexpected parent class path is uncovered.
    """
    from agent.graph.llm.node import validate_stop_reason
    from agent.graph.llm_errors import LLMStreamUnexpectedStopReasonError

    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "pause_turn"},
        usage_metadata={"input_tokens": 10, "output_tokens": 99, "total_tokens": 109},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError) as exc_info:
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))
    assert exc_info.value.stop_reason == "pause_turn", (
        f"stop_reason attribute must be actually set (got {exc_info.value.stop_reason!r}) —— "
        "mutation `stop_reason=None` makes this assertion red"
    )
    assert exc_info.value.output_tokens == 99, (
        f"output_tokens attribute must be actually set (got {exc_info.value.output_tokens!r}) —— "
        "mutation `output_tokens=None` makes this assertion red"
    )


def test_text_display_uses_dot_text_for_list_content() -> None:
    """AIMessage.text on list content extracts plain text, not a stringified list.

    Locks the langchain-normalized `.text` behavior the `_llm_node_impl` display
    line relies on: a Gemini-style list-of-blocks response yields clean text, not
    `str([{"type": "text", "text": "..."}])`.
    """
    msg = AIMessage(content=[{"type": "text", "text": "Two plus two equals four."}])
    assert msg.text == "Two plus two equals four."
    assert not msg.text.startswith("[")  # not str(list)


async def test_cancel_event_set_before_first_chunk_returns_halted_no_publish_done(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
) -> None:
    """cancel_event set immediately (before first chunk) → cancel branch: the entire
    generation is discarded, Command(halted=True, goto=after_exec) does not commit any
    message, and handler.finish() is not called (no LLMDone)."""

    async def _hang_forever() -> AsyncIterator[AIMessageChunk]:
        await asyncio.Future()
        yield  # type: ignore[unreachable]  # pragma: no cover

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_forever()
    agent_id = create_agent(db_conn)
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    # meiva:note
    # Queued before the node starts, so the stream never yields a first chunk.
    queue_command(db_conn, agent_id, "cancel")

    result = await llm_node(
        state,
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
            ops_pool=aops_pool,
        ),
        _config(agent_id),
        ledger=ledger,
        rng=_rng(),
    )
    events = await live_events.typed()

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update is not None
    assert result.update["halted"] is True
    # clean discard: no message committed
    assert result.update.get("messages", []) == []
    # Cancelled event emitted
    assert _has_cancelled_event(events, agent_id)
    # handler.finish() not called → no LLMDone
    assert "llm_done" not in [e.role for e in events]


# ───────────── Silent idle supplementary tests (PR #35 review, agent #976) ─────────────


async def test_silent_idle_with_thinking_blocks_continue_loops(
    hosted_resources: HostedTurnResources, ledger: LlmLedger, database_gate: ProcessDbGate
) -> None:
    """thinking blocks present but output_tokens=0 → still judged as silent idle.

    The first condition of `has_reasoning`: when content contains a type="thinking" block,
    even if output_tokens=0 it should be recognized as "produced reasoning but no action" →
    continue-loop, rather than falling into the truly-empty WARNING + halt path.

    Locks the silent_idle thinking-block condition so it is not coupled with the output_tokens > 0 condition.
    """

    async def _thinking_only_chunk() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content=[{"type": "thinking", "thinking": "Let me reason...", "signature": "sig-x"}],
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _thinking_only_chunk()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state,
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=MagicMock(),
            database_gate=database_gate,
        ),
        CONFIG,
        ledger=ledger,
        rng=_rng(),
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is False
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)


async def test_silent_idle_with_deepseek_reasoning_content_continue_loops(
    hosted_resources: HostedTurnResources, ledger: LlmLedger, database_gate: ProcessDbGate
) -> None:
    """DeepSeek model's reasoning is in `additional_kwargs.reasoning_content`,
    not in Anthropic's content blocks thinking type — silent_idle detection
    must also cover this path, otherwise DeepSeek reasoning-only turn would be missed.

    Construct: output_tokens=0, no text, no tool_call, no thinking blocks,
    but has `additional_kwargs.reasoning_content` → judged silent idle → continue-loop.
    """

    async def _ds_reasoning_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
            additional_kwargs={"reasoning_content": "Let me analyze the request step by step..."},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _ds_reasoning_only()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state,
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=MagicMock(),
            database_gate=database_gate,
        ),
        CONFIG,
        ledger=ledger,
        rng=_rng(),
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is False
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)


async def test_silent_idle_zero_output_reasoning_content_consumes_minimum_budget(
    hosted_resources: HostedTurnResources,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
) -> None:
    """Reasoning-content-only turns cannot bypass the silent-idle cost guard."""

    async def _reasoning_content_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
            additional_kwargs={"reasoning_content": "I still need to think."},
        )

    for turn in range(1, 4):
        fake_llm = MagicMock()
        fake_llm.astream.return_value = _reasoning_content_only()
        result = await llm_node(
            AgentState(messages=[HumanMessage(content="hi")], halted=False),
            make_runtime(
                resources=hosted_resources,
                llm=fake_llm,
                event_publisher=MagicMock(),
                database_gate=database_gate,
                pins={"llm_silent_idle_max_output_tokens": 3},
            ),
            CONFIG,
            ledger=ledger,
            rng=_rng(),
        )
        assert isinstance(result, Command)
        assert result.update["halted"] is (turn == 3)

    assert ledger.silent_idle_output_tokens("7") == 0


# ─────────── provider-error taxonomy: fail-fast (permanent) vs retry (transient) ───────────
# The status→ErrorClass mapping itself is covered exhaustively in
# base/packages/tests/lm/providers/test_provider_errors.py (classify_error). These drive the wiring
# through llm_node: a PERMANENT class becomes a fail-fast FatalProviderError, a
# TRANSIENT class re-raises for the retry loop.


class FakeProviderStatusError(openai.APIStatusError):
    """An actual SDK status error with a synthetic response."""

    def __init__(self, status_code: int, body: object = None) -> None:
        response = httpx2.Response(
            status_code, request=httpx2.Request("POST", "https://audit.invalid")
        )
        super().__init__(f"HTTP {status_code}", response=response, body=body)


def astream_raising(exc: Exception) -> AsyncIterator[AIMessageChunk]:
    async def _gen() -> AsyncIterator[AIMessageChunk]:
        raise exc
        yield  # pragma: no cover — unreachable; only marks this an async generator

    return _gen()


async def test_llm_node_permanent_provider_error_fails_fast_with_structured_fields(
    hosted_resources: HostedTurnResources,
    loguru_records,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
) -> None:
    """A PERMANENT provider error (HTTP 400 — bad request / context length /
    schema) raised mid-stream becomes a FatalProviderError carrying the
    classifier's structured (error_class, provider, status), and the structured
    `llm_provider_error` log lands error_class=permanent / status=400 / fatal=True.
    The retry loop excludes FatalProviderError, so the agent idles instead of
    burning the ~16-min backoff budget and dying."""
    from agent.graph.llm_errors import FatalProviderError

    fake_llm = MagicMock()
    fake_llm.astream.return_value = astream_raising(FakeProviderStatusError(400))
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(FatalProviderError) as exc_info:
        await llm_node(
            state,
            make_runtime(
                resources=hosted_resources,
                llm=fake_llm,
                event_publisher=MagicMock(),
                database_gate=database_gate,
            ),
            CONFIG,
            ledger=ledger,
            rng=_rng(),
        )

    assert exc_info.value.error_class == "permanent"
    assert exc_info.value.status == 400
    classify_logs = [r for r in loguru_records if r["extra"].get("event") == "llm_provider_error"]  # pyright: ignore[reportUnknownMemberType]
    assert len(classify_logs) == 1, "exactly one structured classification log per failed call"  # pyright: ignore[reportUnknownArgumentType]
    assert classify_logs[0]["extra"]["error_class"] == "permanent"
    assert classify_logs[0]["extra"]["status"] == 400
    assert classify_logs[0]["extra"]["fatal"] is True
    # Every provider failure records an explicit billing verdict.
    assert classify_logs[0]["extra"]["billing"] is False
    assert classify_logs[0]["extra"]["model"] == settings.lm.llm_model


# ────────────────────────────────────────────────────────────
# _parse_provider_error_type / is_fatal_provider_error_type
# ────────────────────────────────────────────────────────────


class FakeOpenAIError(openai.APIStatusError):
    """Actual OpenAI SDK authority for configured fatal-type tests."""

    def __init__(self, body: object = None) -> None:
        response = httpx2.Response(429, request=httpx2.Request("POST", "https://audit.invalid"))
        super().__init__("fake error", response=response, body=body)


class FakeAnthropicError(anthropic.APIStatusError):
    """Actual Anthropic SDK authority for configured fatal-type tests."""

    def __init__(self, body: object = None) -> None:
        response = httpx2.Response(429, request=httpx2.Request("POST", "https://audit.invalid"))
        super().__init__("fake error", response=response, body=body)


def fatal(error_type: str | None, configured: str) -> bool:
    """`is_fatal_provider_error_type` for an error of `error_type` under a configured fatal set."""
    from agent.graph.llm_errors import is_fatal_provider_error_type

    body = {"error": {"type": error_type, "message": "m"}}
    policy = LlmCallPolicy(configured)
    return is_fatal_provider_error_type(FakeOpenAIError(body), policy)


__all__ = ["FakeAnthropicError"]
