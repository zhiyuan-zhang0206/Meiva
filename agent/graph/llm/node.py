from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph._callbacks import RedisStreamHandler
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.hooks.compact import auto_compact_for_llm
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LlmLedger,
    LLMRetryBudgetExceededError,
)
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.graph.tool_calls import code_from_args
from agent.hooks.understanding_chunks import due_chunk_update
from agent.llm.usage import log_llm_usage
from agent.nodes import AFTER_EXEC, BEFORE_EXEC
from agent.state_channels import CircuitState
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.messages.kwargs import read_meiva_kwargs
from base.events.live.projection import TokenUsage
from base.events.live.publisher import AgentEventPublisher
from base.lm.catalog import ModelCatalog
from base.lm.reasoning import content_blocks
from base.log import logger

from ._backoff import Attempt, RetryPolicy, assess_failure, budget_spent
from ._cancel import race_stream_vs_cancel
from ._chunk import assemble_final_message
from ._chunk import validate_stop_reason as validate_stop_reason
from ._retry import draw_jitter, record_remaining, retry_policy, retry_wait
from ._stream import stream_llm
from .goto import LlmGoto


def _log_llm_retry_duration(
    attempt: Attempt,
    *,
    outcome: Literal["succeeded", "attempts_exhausted", "budget_exhausted"],
    now: float,
) -> None:
    if attempt.number < 2:
        return
    logger.info(
        "LLM retry sequence {outcome} after {duration_seconds:.2f}s",
        event="llm_retry",
        outcome=outcome,
        duration_seconds=attempt.elapsed_seconds(now),
    )


def _raise_retry_budget_exhausted(attempt: int, policy: RetryPolicy) -> NoReturn:
    raise LLMRetryBudgetExceededError(
        "LLM retry wall-clock budget "
        f"({policy.max_total_seconds:.0f}s) exhausted before attempt {attempt}"
    )


def _finalize_turn_observability(
    publisher: AgentEventPublisher,
    agent_id: int,
    final_msg: AIMessage,
    handler: RedisStreamHandler,
    model: str,
    *,
    catalog: ModelCatalog,
) -> None:
    kw = read_meiva_kwargs(final_msg)
    kw["meiva_created_at"] = datetime.now(UTC).isoformat()
    reasoning_ms_by_block = handler.reasoning_ms_by_block
    if reasoning_ms_by_block:
        kw["meiva_reasoning_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in reasoning_ms_by_block.items()
        }
    code_ms_by_block = handler.code_ms_by_block
    if code_ms_by_block:
        kw["meiva_code_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in code_ms_by_block.items()
        }
    log_llm_usage(
        final_msg,
        model=model,
        agent_id=agent_id,
        catalog=catalog,
        latency_ms=handler.llm_latency_ms,
        decode_ms=handler.llm_decode_ms,
    )
    usage = final_msg.usage_metadata or {}
    from base.lm.reasoning import extract_reasoning_tokens

    reasoning_tokens = extract_reasoning_tokens(
        final_msg.usage_metadata,
        total_reasoning_chars=handler.total_reasoning_chars,
    )
    publisher.emit(
        TokenUsage(
            agent_id=agent_id,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            reasoning_tokens=reasoning_tokens,
        ).model_dump_json()
    )


async def llm_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    *,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    agent_id = agent_id_from_config(config)
    policy = retry_policy(
        runtime.context.require_agent(), runtime.context.require_catalog(), agent_id
    )
    first_started_at = time.time()
    failed = 0
    while True:
        try:
            attempt = Attempt(failed + 1, first_started_at)
            return await llm_attempt(state, runtime, config, attempt, ledger)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            failed += 1
            wait = retry_wait(exc, failed, policy=policy, jitter_seconds=draw_jitter())
            if wait is None:
                raise
            await asyncio.sleep(wait)
            logger.info(
                "Retrying the llm node after {wait:.2f}s (failed try {failed}): {error}",
                wait=wait,
                failed=failed,
                error=f"{type(exc).__name__}: {exc}",
            )


def _enforce_retry_budget(attempt: Attempt, policy: RetryPolicy) -> None:
    now = time.time()
    if budget_spent(attempt, now, policy):
        _log_llm_retry_duration(attempt, outcome="budget_exhausted", now=now)
        _raise_retry_budget_exhausted(attempt.number, policy)


def _note_failed_attempt(
    exc: BaseException,
    attempt: Attempt,
    runtime: Runtime[MeivaContext],
    agent_id: int,
    policy: RetryPolicy,
) -> None:
    runtime.context.turn_progress.mark(agent_id)
    if not isinstance(exc, Exception):
        return
    now = time.time()
    failed = assess_failure(
        attempt,
        now,
        policy,
        budget_error=isinstance(exc, LLMRetryBudgetExceededError),
        fatal=isinstance(exc, (FatalLLMStreamError, FatalProviderError)),
    )
    if failed.ended is not None:
        _log_llm_retry_duration(attempt, outcome=failed.ended, now=now)
    record_remaining(exc, failed.remaining_seconds)


# meiva:note
# Every turn_end record carries its owning agent's id, passed explicitly at the emit site: the process-wide log binding is the no-agent sentinel and the Inspector activity projection drops unattributed records.
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md
async def llm_attempt(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    attempt: Attempt,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    turn_start = time.monotonic()
    agent_id = agent_id_from_config(config)
    event_publisher = runtime.context.event_publisher
    if event_publisher is None:
        raise RuntimeError("llm_node requires ctx.event_publisher")
    policy = retry_policy(
        runtime.context.require_agent(), runtime.context.require_catalog(), agent_id
    )
    async with node_lifecycle(
        "llm",
        messages=state.messages,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=runtime.context.turn_progress,
        stall=StallConfig(runtime.context.require_agent().read("agent", "node_stall_dump_seconds")),
        timeline_inputs=runtime.context.timestamp_policy(),
        timeline_limit=runtime.context.require_agent().read("display", "timeline_default_limit"),
    ):
        try:
            _enforce_retry_budget(attempt, policy)
            result = await _llm_node_impl(state, runtime, config, ledger)
        except BaseException as exc:
            _note_failed_attempt(exc, attempt, runtime, agent_id, policy)
            logger.opt(exception=True).warning(
                "turn ended in {duration_seconds:.2f}s ok=False — _llm_node_impl raised",
                event="turn_end",
                agent_id=agent_id,
                duration_seconds=time.monotonic() - turn_start,
                ok=False,
            )
            raise
        else:
            _log_llm_retry_duration(attempt, outcome="succeeded", now=time.time())
            logger.info(
                "turn ended in {duration_seconds:.2f}s ok=True",
                event="turn_end",
                agent_id=agent_id,
                duration_seconds=time.monotonic() - turn_start,
                ok=True,
            )
            update = result.update
            if state.circuit.open and update is not None and "messages" in update:
                update["circuit"] = CircuitState()
                logger.info(
                    "heartbeat circuit breaker CLOSED — LLM call accepted again",
                    event="circuit_breaker_closed",
                    agent_id=agent_id_from_config(config),
                )
            return result


def _is_silent_idle(final_msg: AIMessage) -> bool:
    if final_msg.tool_calls or final_msg.text:
        return False
    output_tokens = (final_msg.usage_metadata or {}).get("output_tokens", 0)
    _content: Any = final_msg.content
    has_thinking_block = isinstance(_content, list) and any(
        isinstance(b, dict) and cast(dict[str, Any], b).get("type") == "thinking"
        for b in content_blocks(cast(list[Any], _content))
    )
    return (
        has_thinking_block
        or bool(final_msg.additional_kwargs.get("reasoning_content"))
        or output_tokens > 0
    )


def _silent_idle_command(
    final_msg: AIMessage,
    agent_id: int,
    model: str,
    ledger: LlmLedger,
    *,
    catalog: ModelCatalog,
    cap: int,
) -> Command[LlmGoto] | None:
    if not _is_silent_idle(final_msg):
        return None
    tid = str(agent_id)
    usage = final_msg.usage_metadata or {}
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    budget_tokens = max(output_tokens, 1)
    cumulative_output_tokens = ledger.silent_idle_output_tokens(tid) + budget_tokens
    from base.lm.pricing import quote

    priced = quote(model, 0, output_tokens, 0, prices=catalog.prices)
    estimated_cost_usd = priced.cost_usd if priced is not None else None
    if cap > 0 and cumulative_output_tokens >= cap:
        ledger.reset_silent_idle(tid)
        logger.warning(
            "[{label}] {body}",
            label="silent-idle",
            event="silent_idle",
            body=(
                f"reasoning-only output reached {cumulative_output_tokens} budget tokens (cap {cap}) — "
                "halting to idle instead of looping"
            ),
            output_tokens=output_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            halted=True,
        )
        return Command[LlmGoto](
            update={"messages": [final_msg], "halted": True},
            goto=AFTER_EXEC,
        )
    ledger.record_silent_idle_output_tokens(tid, cumulative_output_tokens)
    logger.info(
        "[{label}] {body}",
        label="silent-idle",
        event="silent_idle",
        body=(
            f"reasoning-only output {output_tokens} tokens "
            f"(budget {budget_tokens}; cumulative {cumulative_output_tokens}/{cap or 'inf'}) "
            "— continue-loop with nudge"
        ),
        output_tokens=output_tokens,
        cumulative_output_tokens=cumulative_output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        halted=False,
    )
    return Command[LlmGoto](
        update={"messages": [final_msg], "halted": False},
        goto=AFTER_EXEC,
    )


async def _llm_node_impl(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    ctx = runtime.context
    if ctx.event_publisher is None:
        raise RuntimeError("_llm_node_impl requires ctx.event_publisher")
    if ctx.llm is None:
        raise RuntimeError("_llm_node_impl requires ctx.llm")
    publisher = ctx.event_publisher
    llm = ctx.llm
    agent_id = agent_id_from_config(config)

    compacted = await auto_compact_for_llm(state, runtime, config)
    if compacted is not None:
        goto = compacted.pop("goto")
        return Command[LlmGoto](update=compacted, goto=goto)

    handler = RedisStreamHandler(
        publisher,
        agent_id,
        msg_idx=len(state.messages),
        turn_progress=ctx.turn_progress,
    )

    chunks: list[AIMessageChunk] = []

    cancelled_cmd = await race_stream_vs_cancel(
        ctx,
        agent_id,
        stream_llm(
            llm,
            list(state.messages),
            chunks=chunks,
            handler=handler,
            agent=ctx.require_agent(),
            catalog=ctx.require_catalog(),
        ),
        handler,
    )
    if cancelled_cmd is not None:
        return cancelled_cmd

    if not chunks:
        return Command[LlmGoto](update={"messages": [AIMessage(content="")]}, goto=BEFORE_EXEC)
    final_msg = assemble_final_message(chunks, catalog=ctx.require_catalog())

    text = final_msg.text
    if text:
        logger.info("[text] {body}", event="text", body=text)
    await persist_last_active(ctx, agent_id, text)
    _finalize_turn_observability(
        publisher,
        agent_id,
        final_msg,
        handler,
        ctx.require_agent().brain.llm_model,
        catalog=ctx.require_catalog(),
    )

    cut_update = await due_chunk_update(
        state.compact,
        list(state.messages),
        final_msg,
        pool=ctx.ops_pool,
        agent_id=agent_id,
        agent=ctx.require_agent(),
        catalog=ctx.require_catalog(),
    )

    silent_idle_cmd = _silent_idle_command(
        final_msg,
        agent_id,
        ctx.require_agent().brain.llm_model,
        ledger,
        catalog=ctx.require_catalog(),
        cap=ctx.require_agent().read("lm", "llm_silent_idle_max_output_tokens"),
    )
    if silent_idle_cmd is not None:
        return Command[LlmGoto](
            update={**cast("dict[str, Any]", silent_idle_cmd.update), **cut_update},
            goto=silent_idle_cmd.goto,
        )

    ledger.reset_silent_idle(str(agent_id))

    if not final_msg.tool_calls:
        if not text:
            logger.warning(
                "[halt] {body}",
                event="halt",
                body="no tool_call and EMPTY text — empty turn, user sees no reply",
                agent_id=agent_id,
            )
        else:
            logger.info(
                "[halt] {body}", event="halt", body="no tool_call (idle)", agent_id=agent_id
            )
        return Command[LlmGoto](
            update={"messages": [final_msg], "halted": True, **cut_update},
            goto=AFTER_EXEC,
        )
    logger.info(
        "[code] {body}",
        event="code",
        body=code_from_args(final_msg.tool_calls[0]["args"], source="llm final_msg tool_call"),
        agent_id=agent_id,
    )
    return Command[LlmGoto](update={"messages": [final_msg], **cut_update}, goto=BEFORE_EXEC)
