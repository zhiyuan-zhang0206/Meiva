# meiva:note
# End-to-end hosted turn-abort and resource-retention boundary tests over real PostgreSQL,
# Redis, `build_graph`, `build_checkpointer`, and `AgentHost.run_turn`. Reaches a scripted
# `BaseChatModel` through the production `llm_override` seam (`module:build_test_llm`) so
# `claim_node`, `llm_node`, `PendingTurnFailure`, `settle_turn_failure`, `stall_guard`, and
# `HostedServiceResources` all run unmodified.
# meiva:link services/docs/agent_runner_side/agent_runner_side.meiva.okf.md

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic
import httpx2
import psycopg
import pytest
from langchain_core.exceptions import ModelConnectionError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from psycopg_pool import AsyncConnectionPool

from agent.graph import build_graph
from agent.graph.exec.tests.process_inputs import live_parent_facts
from agent.state import CircuitState
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.inbox.insert import insert_inbound_message
from base.db import Database
from base.events.live.bus import EventBus
from base.log.tests.log_records import LogRecords
from base.packages.plugins.extensions import ExtensionRegistry
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import StallTiming
from services.agent_runner.agent_host.stall_guard import TurnStallTimeoutError
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import (
    BuildWith,
    EventTap,
    GatedGraph,
    Rig,
    Row,
)

OVERRIDE = f"{__name__}:build_test_llm"

StreamAttempt = Sequence[AIMessageChunk | BaseException]
PLANS: dict[int, list[StreamAttempt]] = {}
ATTEMPT_COUNTS: dict[int, int] = {}


# meiva:note
# Deterministic PRNG seed for non-cryptographic retry jitter in tests (`S311`).
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


class _PlannedStreamModel(BaseChatModel):
    agent_id: int

    @property
    def _llm_type(self) -> str:
        return "planned-stream-test"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _PlannedStreamModel:
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("the hosted graph streams the model asynchronously")

    async def _astream(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, args, kwargs
        ATTEMPT_COUNTS[self.agent_id] = ATTEMPT_COUNTS.get(self.agent_id, 0) + 1
        steps = PLANS[self.agent_id].pop(0)
        for step in steps:
            if isinstance(step, BaseException):
                raise step
            yield ChatGenerationChunk(message=step)


def build_test_llm(model: str, *, agent_id: int | None) -> _PlannedStreamModel:
    del model
    if agent_id is None:
        raise ValueError("the planned stream test model requires an agent_id")
    return _PlannedStreamModel(agent_id=agent_id)


def _anthropic_503() -> anthropic.InternalServerError:
    response = httpx2.Response(503, request=httpx2.Request("POST", "https://audit.invalid"))
    return anthropic.InternalServerError(
        "Service unavailable",
        response=response,
        body={
            "type": "error",
            "error": {"type": "service_unavailable_error", "message": "Service unavailable"},
        },
    )


async def _cold_checkpoint_values(pool: AsyncConnectionPool[Any], agent_id: int) -> dict[str, Any]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    reader = AsyncPostgresSaver(pool)
    wrap_saver_reads_with_delta_reconstruction(reader)
    saved = await reader.aget_tuple({"configurable": {"thread_id": str(agent_id)}})
    assert saved is not None
    return dict(saved.checkpoint["channel_values"])


def _assert_no_late_resource_failure_logs(loguru_records: LogRecords) -> None:
    assert not any(
        "late hosted resource failure" in record["message"]
        or "retained until service stop/join" in record["message"]
        for record in loguru_records
    )


async def _assert_settled_turn_abort(
    host: AgentHost,
    pool: AsyncConnectionPool[Any],
    event_tap: EventTap,
    loguru_records: LogRecords,
    *,
    agent_id: int,
    expected_snippet: str | None = None,
) -> dict[str, Any]:
    values = await _cold_checkpoint_values(pool, agent_id)
    assert values["halted"] is True
    raw_circuit = values.get("circuit")
    if raw_circuit is not None:
        assert not CircuitState.model_validate(raw_circuit).open
    errors = await event_tap.events("error")
    assert len(errors) == 1
    assert not errors[0].get("blocked", False)
    if expected_snippet is not None:
        assert expected_snippet in errors[0]["content"]
    aborts = loguru_records.events("llm_turn_aborted")
    assert len(aborts) == 1
    assert aborts[0]["exception"] is not None
    assert loguru_records.events("host_turn_crashed") == []
    assert loguru_records.events("circuit_breaker_open") == []
    assert host._resource_service.failures == []
    _assert_no_late_resource_failure_logs(loguru_records)
    return aborts[0]


@pytest.mark.parametrize(
    ("case_name", "chunks", "expected_snippet"),
    [
        (
            "eof_drop",
            [
                AIMessageChunk(
                    content="partial output before drop",
                    response_metadata={"model_provider": "anthropic"},
                )
            ],
            "terminal reason missing",
        ),
        (
            "end_turn_missing_usage",
            [
                AIMessageChunk(
                    content="hello",
                    response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                )
            ],
            "final_msg.usage_metadata is None",
        ),
        (
            "max_tokens_truncated",
            [
                AIMessageChunk(
                    content="truncated answer",
                    response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"},
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 4096,
                        "total_tokens": 4106,
                    },
                )
            ],
            "max_tokens",
        ),
    ],
)
async def test_hosted_turn_settles_stream_corruption_and_truncation_cleanly(
    case_name: str,
    chunks: list[AIMessageChunk],
    expected_snippet: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    event_tap: EventTap,
    loguru_records: LogRecords,
    rig: Rig,
) -> None:
    del case_name
    agent_id = 1
    PLANS[agent_id] = [chunks]
    ATTEMPT_COUNTS[agent_id] = 0
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=_rng())
    host = rig.host(
        {agent_id: Row(status="idling")},
        graph,
        catalog=None,
        policy=configured_policy(llm_override=OVERRIDE),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    insert_inbound_message(
        db_conn, agent_id, "Run the turn", "user", bus=event_bus, database=database
    )
    try:
        await asyncio.wait_for(host.run_turn(agent_id), 15)
        assert ATTEMPT_COUNTS[agent_id] == 1
        await _assert_settled_turn_abort(
            host,
            aops_pool,
            event_tap,
            loguru_records,
            agent_id=agent_id,
            expected_snippet=expected_snippet,
        )
    finally:
        await host.aclose()
    assert host.resources_joined


@pytest.mark.parametrize(
    ("attempts", "expected_provider", "expected_status"),
    [
        ([_anthropic_503, _anthropic_503], "anthropic", 503),
        (
            [
                lambda: ModelConnectionError("upstream connection reset"),
                lambda: ModelConnectionError("upstream connection reset"),
            ],
            "langchain_core",
            None,
        ),
    ],
)
async def test_hosted_turn_settles_exhausted_transient_provider_retries_cleanly(
    attempts: list[Any],
    expected_provider: str | None,
    expected_status: int | None,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    event_tap: EventTap,
    loguru_records: LogRecords,
    rig: Rig,
) -> None:
    agent_id = 1
    PLANS[agent_id] = [[factory()] for factory in attempts]
    ATTEMPT_COUNTS[agent_id] = 0
    overlay = {
        "llm_retry_max_attempts": 2,
        "llm_retry_max_total_seconds": 60.0,
        "llm_retry_initial_interval_seconds": 0.01,
        "llm_retry_max_interval_seconds": 0.02,
    }
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=_rng())
    host = rig.host(
        {agent_id: Row(status="idling", overlay=overlay)},
        graph,
        catalog=None,
        policy=configured_policy(llm_override=OVERRIDE),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    insert_inbound_message(
        db_conn,
        agent_id,
        "Run transient retry exhaustion turn",
        "user",
        bus=event_bus,
        database=database,
    )
    try:
        await asyncio.wait_for(host.run_turn(agent_id), 15)
        assert ATTEMPT_COUNTS[agent_id] == 2
        abort_record = await _assert_settled_turn_abort(
            host,
            aops_pool,
            event_tap,
            loguru_records,
            agent_id=agent_id,
        )
        assert abort_record["extra"]["error_class"] == "transient"
        assert abort_record["extra"]["provider"] == expected_provider
        assert abort_record["extra"]["status"] == expected_status
    finally:
        await host.aclose()
    assert host.resources_joined


async def test_hosted_turn_stall_timeout_propagates_without_polluting_service_failures(
    wired_with: BuildWith,
    loguru_records: LogRecords,
) -> None:
    stall = StallTiming(scan_interval_s=0.01, no_progress_timeout_s=0.02, unwind_timeout_s=1.0)
    host, graph, stored = wired_with(
        {1: Row(status="idling")},
        GatedGraph(),
        policy=configured_policy(stall=stall),
    )
    try:
        with pytest.raises(TurnStallTimeoutError):
            await asyncio.wait_for(host.run_turn(1), 5)
        assert graph.cancel_seen
        assert stored.status(1) == "idling"
        assert host._resource_service.failures == []
        _assert_no_late_resource_failure_logs(loguru_records)
    finally:
        await host.aclose()
    assert host.resources_joined
