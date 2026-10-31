# meiva:note
# `llm_node` turn_end finally-path unit tests — ok=False MUST carry a traceback. 161 incident: turn_end ok=False but neither events nor stderr had exception info — `logger.info` in the finally block emitted but didn't capture exc_info, so from the framework's perspective "why it failed" was completely invisible. Fix: `logger.opt(exception=True).warning(...)` on the ok=False path, together with `base.telemetry.process_logging._event_sink` automatically injecting traceback / exception_type / exception_value into events.payload. Tests focus on the two branches of the finally block (ok=True / ok=False): the model the node streams from fails, or answers.

from __future__ import annotations

import random
import time

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from agent.graph.llm._backoff import Attempt
from agent.graph.llm.node import llm_attempt, llm_node
from agent.graph.llm.tests.llm_support import ScriptedModel, end_turn_chunk, llm_runtime
from agent.state import AgentState
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


_STATE = AgentState(messages=[HumanMessage(content="hi")], halted=False)


def _config_with_thread() -> RunnableConfig:
    """agent_id is read from config["configurable"]["thread_id"], must be indexable to str."""
    return {"configurable": {"thread_id": "1"}}


# meiva:note
# The model fails; the finally path uses logger.opt(exception=True) so that record["exception"] is not None, allowing downstream _event_sink to inject the traceback.
async def test_turn_end_ok_false_record_carries_exception(
    loguru_records: LogRecords, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(scripts=[[RuntimeError("simulated LLM timeout 39s")]])

    with pytest.raises(RuntimeError, match="simulated LLM timeout"):
        await llm_attempt(
            _STATE,
            llm_runtime(model, model_catalog=model_catalog, database_gate=database_gate),
            _config_with_thread(),
            Attempt(1, time.time()),
        )

    turn_end_records = loguru_records.events("turn_end")
    assert len(turn_end_records) == 1
    rec = turn_end_records[0]
    assert rec["extra"]["ok"] is False
    assert (
        rec["level"].name == "WARNING"  # pyright: ignore[reportUnknownMemberType]
    )  # ok=False goes through warning, distinct from ok=True info
    assert rec["exception"] is not None, (
        "logger.opt(exception=True) must attach an exception object — _event_sink relies on it to inject traceback"
    )
    assert rec["exception"].type is RuntimeError  # pyright: ignore[reportUnknownMemberType]
    assert "simulated LLM timeout" in str(rec["exception"].value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


async def test_turn_end_ok_true_record_no_exception(
    loguru_records: LogRecords, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """Normal path ok=True: uses logger.info without exc_info, record["exception"] is None,
    payload must not bloat with traceback/exception_* fields."""
    model = ScriptedModel(scripts=[[end_turn_chunk()]])

    result = await llm_node(
        _STATE,
        llm_runtime(model, model_catalog=model_catalog, database_gate=database_gate),
        _config_with_thread(),
        rng=_rng(),
    )

    assert result.update is not None and result.update["messages"][0].content == "ok"
    turn_end_records = loguru_records.events("turn_end")
    assert len(turn_end_records) == 1
    rec = turn_end_records[0]
    assert rec["extra"]["ok"] is True
    assert rec["level"].name == "INFO"  # pyright: ignore[reportUnknownMemberType]
    assert rec["exception"] is None
    assert "meiva_non_human_wake" not in result.update["messages"][0].additional_kwargs


async def test_llm_node_stamps_meiva_non_human_wake_only_when_state_non_human_wake(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(scripts=[[end_turn_chunk("internal ack")]])
    bg_state = AgentState(
        messages=[HumanMessage(content="[agent:8] done")],
        halted=False,
        non_human_wake=True,
    )

    result = await llm_node(
        bg_state,
        llm_runtime(model, model_catalog=model_catalog, database_gate=database_gate),
        _config_with_thread(),
        rng=_rng(),
    )

    assert result.update is not None
    ai_msg = result.update["messages"][0]
    assert ai_msg.additional_kwargs.get("meiva_non_human_wake") is True
