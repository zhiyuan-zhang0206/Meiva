# meiva:note
# The llm node's own retry loop (agent/graph/llm/_retry.py and node.py): which failures retry, the policy an agent's config and model give it, and the loop that applies it. The agent host builds ONE graph for every local agent, so the schedule is decided per failure from the agent's model and id, never from a graph-level policy. The timing arithmetic itself is in test_backoff.py.
from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.exceptions import ModelConnectionError
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from agent.graph.hooks.compact import CompactionFailedError
from agent.graph.llm import node
from agent.graph.llm._backoff import WAIT_JITTER_SPAN_S
from agent.graph.llm._retry import record_remaining, retry_policy, retry_wait
from agent.graph.llm.tests.llm_support import ScriptedModel, end_turn_chunk, llm_runtime
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LlmLedger,
    LLMStreamCorruptedError,
    LLMStreamStallTimeoutError,
    LLMStreamTruncatedError,
    LLMStreamUnexpectedStopReasonError,
)
from agent.state import AgentState
from base.config.tests.configuration import defaults
from base.db.code_version_gate import ProcessDbGate
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.registry import ModelSpec, ModelTuning
from base.log.tests.log_records import LogRecords
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws; a seed names the draw a
# test expects.
def _seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


_MODEL = "deepseek-flash"
# meiva:note
# agent_id % 1000 == 0: the transient schedule starts unshifted.
_ZERO_PHASE = 1000
_STATE = AgentState(messages=[HumanMessage(content="hi")], halted=False)
_QUICK = {
    "llm_retry_initial_interval_seconds": 0.01,
    "llm_retry_max_interval_seconds": 0.02,
}


def _policy(agent_id: int = _ZERO_PHASE, **pins: Any):
    agent = AgentSlices.resolve({"llm_model": _MODEL, **pins}, default_reader=defaults())

    return retry_policy(agent, build_model_catalog(home=meiva_home()), agent_id)


def _wait(exc: Exception, failed: int = 1) -> float | None:
    return retry_wait(exc, failed, policy=_policy(), jitter_seconds=0.0)


def test_fatal_and_compaction_failures_are_never_retried() -> None:
    for exc in (
        FatalLLMStreamError("cap exhausted"),
        FatalProviderError("out of balance"),
        CompactionFailedError("summary retry budget exhausted"),
    ):
        assert _wait(exc) is None, type(exc).__name__


def test_transient_failures_are_retried_and_stalls_are_not() -> None:
    assert _wait(ModelConnectionError("network")) is not None
    assert _wait(LLMStreamStallTimeoutError("stall")) is None


# meiva:note
# A stall must ride the host's fatal-stream path, not the generic crash path:
# the hosted runner routes only `FatalLLMStreamError` / `FatalProviderError` /
# `CompactionFailedError` into the settled turn abort (`halted=True` plus the
# user-visible Error event), while a plain `LLMStreamError` was only logged as a
# crashed turn — the row settled to idling with no message and no error anyone
# could see (agent 6933, 2026-10-18).
# meiva:link agent/graph/llm/docs/llm.meiva.okf.md
def test_a_stream_stall_is_fatal_so_the_turn_abort_is_announced() -> None:
    stall = LLMStreamStallTimeoutError("LLM stream stalled", stage="ttft")

    assert isinstance(stall, FatalLLMStreamError)
    assert _wait(stall) is None, "a stall must never re-stream the request"


def test_an_unknown_failure_is_not_retried() -> None:
    assert _wait(RuntimeError("unknown")) is None


def test_a_failure_that_recorded_a_spent_budget_ends_the_retries() -> None:
    exc = ModelConnectionError("budget exhausted")
    record_remaining(exc, 0.0)

    assert _wait(exc) is None


def test_the_budget_a_failed_try_recorded_clips_the_wait() -> None:
    exc = ModelConnectionError("retryable")
    record_remaining(exc, 1.5)

    assert retry_wait(exc, 1, policy=_policy(), jitter_seconds=0.0) == 0.5


def test_the_policy_is_the_agents_lm_config() -> None:
    policy = _policy(
        42,
        llm_retry_max_total_seconds=99.0,
        llm_retry_initial_interval_seconds=2.0,
        llm_retry_max_interval_seconds=8.0,
    )

    assert (
        policy.agent_id,
        policy.max_total_seconds,
        policy.initial_interval_seconds,
        policy.max_interval_seconds,
    ) == (42, 99.0, 2.0, 8.0)


def test_two_agents_with_different_config_get_their_own_schedules() -> None:
    first, second = (
        _policy(2000, llm_retry_initial_interval_seconds=2.0),
        _policy(2000, llm_retry_initial_interval_seconds=7.0),
    )
    failure = ModelConnectionError("provider transport unavailable")

    assert retry_wait(failure, 1, policy=first, jitter_seconds=0.0) == 2.0
    assert retry_wait(failure, 1, policy=second, jitter_seconds=0.0) == 7.0


def test_the_model_caps_the_number_of_tries(model_catalog: ModelCatalog) -> None:
    catalog = replace(
        model_catalog,
        models={
            **model_catalog.models,
            "model-a": ModelSpec(provider="test", tuning=ModelTuning(llm_retry_max_attempts=2)),
            "model-b": ModelSpec(provider="test", tuning=ModelTuning(llm_retry_max_attempts=4)),
        },
    )

    def cap(model: str, **pins: Any) -> int:
        agent = AgentSlices.resolve({"llm_model": model, **pins}, default_reader=defaults())
        return retry_policy(agent, catalog, 1).max_attempts

    assert (cap("model-a"), cap("model-b")) == (2, 4)
    assert cap("model-a", llm_retry_max_attempts=7) == 7


def test_the_try_cap_of_the_model_ends_the_retries_at_that_failure(
    model_catalog: ModelCatalog,
) -> None:
    catalog = replace(
        model_catalog,
        models={
            **model_catalog.models,
            "model-a": ModelSpec(provider="test", tuning=ModelTuning(llm_retry_max_attempts=2)),
        },
    )
    policy = retry_policy(
        AgentSlices.resolve({"llm_model": "model-a"}, default_reader=defaults()), catalog, 1000
    )
    failure = ModelConnectionError("x")

    waits = [retry_wait(failure, failed, policy=policy, jitter_seconds=0.0) for failed in (1, 2)]

    assert waits[0] is not None and waits[1] is None


async def _node_run(
    model: ScriptedModel,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    thread: str = "1000",
    seed: int = 0,
    **pins: Any,
) -> Any:
    runtime = llm_runtime(
        model, model_catalog=model_catalog, database_gate=database_gate, pins={**_QUICK, **pins}
    )
    config: RunnableConfig = {"configurable": {"thread_id": thread}}
    return await node.llm_node(_STATE, runtime, config, ledger=LlmLedger(), rng=_seeded(seed))


async def test_the_node_retries_a_transient_failure_and_returns_the_next_success(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate, loguru_records: LogRecords
) -> None:
    model = ScriptedModel(scripts=[[ModelConnectionError("a")], [end_turn_chunk("recovered")]])

    result = await _node_run(model, model_catalog=model_catalog, database_gate=database_gate)

    assert result.update["messages"][0].content == "recovered"
    assert model.scripts == []
    retries = loguru_records.events("llm_retry")
    assert [record["extra"]["outcome"] for record in retries] == ["succeeded"]


# meiva:note
# Two runs that differ only in their generator wait exactly as long as the draws differ.
async def test_the_node_draws_its_retry_jitter_from_the_generator_it_is_given(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate, loguru_records: LogRecords
) -> None:
    waits: list[float] = []
    for seed in (31, 43):
        model = ScriptedModel(scripts=[[ModelConnectionError("a")], [end_turn_chunk("ok")]])
        mark = len(loguru_records)
        await _node_run(
            model,
            model_catalog=model_catalog,
            database_gate=database_gate,
            seed=seed,
        )
        retried = [
            record["extra"]["wait"]
            for record in loguru_records[mark:]
            if record["message"].startswith("Retrying the llm node")
        ]
        assert len(retried) == 1
        waits.append(retried[0])

    drawn = [_seeded(seed).uniform(0, WAIT_JITTER_SPAN_S) for seed in (31, 43)]
    assert waits[1] - waits[0] == pytest.approx(drawn[1] - drawn[0])


async def test_the_node_does_not_retry_a_fatal_failure(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    unreached = [end_turn_chunk("a retry would have streamed this")]
    model = ScriptedModel(scripts=[[FatalProviderError("balance")], unreached])

    with pytest.raises(FatalProviderError):
        await _node_run(model, model_catalog=model_catalog, database_gate=database_gate)

    assert model.scripts == [unreached]


@pytest.mark.parametrize(
    "error",
    [
        LLMStreamStallTimeoutError("stall"),
        TypeError("programming error"),
        ValueError("invalid input"),
        ConnectionError("another service"),
        TimeoutError("another operation"),
        LLMStreamCorruptedError("missing terminal field"),
        LLMStreamUnexpectedStopReasonError("refused", stop_reason="unknown", output_tokens=3),
        LLMStreamTruncatedError("truncated", stop_reason="length", output_tokens=3),
    ],
)
async def test_the_node_does_not_repeat_unknown_or_protocol_failures(
    error: Exception, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    unreached = [end_turn_chunk("must not execute")]
    model = ScriptedModel(scripts=[[error], unreached])

    with pytest.raises(type(error)) as raised:
        await _node_run(model, model_catalog=model_catalog, database_gate=database_gate)

    assert raised.value is error
    assert model.scripts == [unreached]


async def test_the_node_gives_up_at_the_models_attempt_cap(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(scripts=[[ModelConnectionError(str(i))] for i in range(5)])

    with pytest.raises(ModelConnectionError) as raised:
        await _node_run(
            model,
            model_catalog=model_catalog,
            database_gate=database_gate,
            llm_retry_max_attempts=2,
        )

    assert str(raised.value) == "1"
    assert len(model.scripts) == 3


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_the_node_never_retries_an_interrupt_or_an_exit(
    exc: BaseException, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """Not `Exception`s: they leave the loop at once, as they left the former policy's
    `retry_on`."""
    unreached = [end_turn_chunk("must not run")]
    model = ScriptedModel(scripts=[[exc], unreached])

    with pytest.raises(type(exc)):
        asyncio.run(_node_run(model, model_catalog=model_catalog, database_gate=database_gate))

    assert model.scripts == [unreached]
