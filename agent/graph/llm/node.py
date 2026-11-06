from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, NoReturn, TypeGuard, get_args

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    BaseMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph._callbacks import RedisStreamHandler
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.hooks.compact import auto_compact_for_llm
from agent.graph.hooks.user_delivery import (
    capture_reply,
    delivery_end_reminder,
    with_pending_deliveries,
)
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LLMRetryBudgetExceededError,
    LLMStreamTruncatedError,
)
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.graph.tool_calls import code_from_args
from agent.hooks.understanding_chunks import due_chunk_update
from agent.llm.usage import log_llm_usage
from agent.messages.reply_visibility import has_invalid_reply_visibility, reply_visibility_note
from agent.nodes import AFTER_EXEC, BEFORE_EXEC, BEFORE_LLM
from agent.state_channels import CircuitState
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.messages.kwargs import NoteTag, read_meiva_kwargs
from base.agents.messages.reply_intent import REPLY_VISIBILITY_RULE
from base.agents.observation.ended_with import BUDGET
from base.clock.ids import new_uuid
from base.events.live.projection import TokenUsage
from base.events.live.publisher import AgentEventPublisher
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_mapping, object_of
from base.lm.catalog import ModelCatalog
from base.lm.content import message_content
from base.log import logger

from ._backoff import WAIT_JITTER_SPAN_S, Attempt, RetryPolicy, assess_failure, budget_spent
from ._cancel import race_stream_vs_cancel
from ._chunk import assemble_final_message
from ._chunk import validate_stop_reason as validate_stop_reason
from ._retry import record_remaining, retry_policy, retry_wait
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
    now: datetime,
) -> None:
    kw = read_meiva_kwargs(final_msg)
    kw["meiva_created_at"] = now.isoformat()
    kw["meiva_explicit_visibility"] = handler.delivery_mode == "prefix"
    if handler.delivery_mode is not None:
        kw["meiva_user_delivery_mode"] = handler.delivery_mode
    if handler.non_human_wake:
        kw["meiva_non_human_wake"] = True
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
    rng: random.Random,
) -> Command[LlmGoto]:
    agent_id = agent_id_from_config(config)
    policy = retry_policy(
        runtime.context.require_agent(), runtime.context.require_catalog(), agent_id
    )
    first_started_at = runtime.context.require_clock().now().timestamp()
    failed = 0
    while True:
        try:
            attempt = Attempt(failed + 1, first_started_at)
            return await llm_attempt(state, runtime, config, attempt)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            failed += 1
            jitter_seconds = rng.uniform(0, WAIT_JITTER_SPAN_S)
            wait = retry_wait(exc, failed, policy=policy, jitter_seconds=jitter_seconds)
            if wait is None:
                raise
            await asyncio.sleep(wait)
            logger.info(
                "Retrying the llm node after {wait:.2f}s (failed try {failed}): {error}",
                wait=wait,
                failed=failed,
                error=f"{type(exc).__name__}: {exc}",
            )


def _enforce_retry_budget(attempt: Attempt, policy: RetryPolicy, *, now: float) -> None:
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
    now = runtime.context.require_clock().now().timestamp()
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
# `_model_halted` decides which attempts hold their record for the turn boundary: the ones
# that stopped the model loop themselves. A cancelled attempt returns halted too, but commits
# no message — its partial generation was discarded — so it is not a turn-end decision and
# keeps the plain record.
def _model_halted(update: object) -> bool:
    if not is_object_dict(update):
        return False
    return update.get("halted") is True and "messages" in update


# meiva:note
# A compact update names its `goto` as a plain value; prove it is one of the llm node's routing
# values (`LlmGoto` itself is the owner of that list) before the graph routes on it.
def _is_llm_goto(value: object) -> TypeGuard[LlmGoto]:
    return isinstance(value, str) and value in get_args(LlmGoto)


# meiva:note
# Whether a budget rail, rather than a provider or a code fault, ended the turn.
def _spent_budget(exc: BaseException) -> bool:
    return isinstance(exc, (LLMRetryBudgetExceededError, LLMStreamTruncatedError))


# meiva:note
# Every turn_end record carries its owning agent's id, passed explicitly at the emit site: the process-wide log binding is the no-agent sentinel and the Inspector activity projection drops unattributed records. The record of an attempt that halts the model loop is emitted by the turn boundary instead (claim), which is the only place that knows whether the turn ended there and the reason it ended with.
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md
async def llm_attempt(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    attempt: Attempt,
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
            _enforce_retry_budget(
                attempt,
                policy,
                now=runtime.context.require_clock().now().timestamp(),
            )
            result = await _llm_node_impl(state, runtime, config)
        except BaseException as exc:
            _note_failed_attempt(exc, attempt, runtime, agent_id, policy)
            logger.opt(exception=True).warning(
                "turn ended in {duration_seconds:.2f}s ok=False — _llm_node_impl raised",
                event="turn_end",
                agent_id=agent_id,
                duration_seconds=time.monotonic() - turn_start,
                ok=False,
                **({"ended_with": BUDGET} if _spent_budget(exc) else {}),
            )
            raise
        else:
            _log_llm_retry_duration(
                attempt,
                outcome="succeeded",
                now=runtime.context.require_clock().now().timestamp(),
            )
            # meiva:note
            # `Command.update` is typed `Any` by langgraph; read it as `object` and narrow once,
            # so the halt predicate and the two channel writes below stay checked.
            update = object_of(getattr, result, "update")
            if is_object_dict(update) and _model_halted(update):
                # meiva:note
                # The model stopped on its own, so this attempt's record belongs to the turn
                # boundary: only claim knows whether the turn ends here, and only the agent
                # knows why it stopped. Hold the measured call; the boundary emits it once.
                update["turn_end_held_seconds"] = time.monotonic() - turn_start
            else:
                logger.info(
                    "turn ended in {duration_seconds:.2f}s ok=True",
                    event="turn_end",
                    agent_id=agent_id,
                    duration_seconds=time.monotonic() - turn_start,
                    ok=True,
                )
            if state.circuit.open and is_object_dict(update) and "messages" in update:
                update["circuit"] = CircuitState()
                logger.info(
                    "heartbeat circuit breaker CLOSED — LLM call accepted again",
                    event="circuit_breaker_closed",
                    agent_id=agent_id_from_config(config),
                )
            return result


def _is_reply_correction(messages: Sequence[BaseMessage]) -> bool:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return False
        kwargs = read_meiva_kwargs(message)
        if kwargs.get("meiva_note_tag") == NoteTag.REPLY_VISIBILITY:
            return True
        if kwargs.get("meiva_inbound_id") is not None:
            return False
    return False


def _messages_with_current_visibility_rule(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    messages = list(messages)
    if messages and isinstance(head := messages[0], SystemMessage):
        content = message_content(head)
        if isinstance(content, str) and "## Output visibility by wake source" in content:
            start = content.index("## Output visibility by wake source")
            end = content.find("\n\nOn a new question", start)
            if end >= 0:
                updated = (
                    content[:start]
                    + REPLY_VISIBILITY_RULE
                    + content[end:]
                    + "\n\nThe visibility rule above supersedes any earlier reply-prefix instructions."
                )
                messages[0] = head.model_copy(update={"content": updated})
    return messages


async def _llm_node_impl(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
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
        goto = object_mapping(compacted).get("goto")
        if not _is_llm_goto(goto):
            raise TypeError(f"a compact update must name one of {get_args(LlmGoto)}, not {goto!r}")
        # meiva:note
        # The routing value rides the command, not the state update: drop it from the delta
        # before the graph applies the rest of the compact hook's channels.
        del compacted["goto"]
        return Command[LlmGoto](update=compacted, goto=goto)

    message_id = str(new_uuid(ctx.require_rng()))
    handler = RedisStreamHandler(
        publisher,
        agent_id,
        msg_idx=len(state.messages),
        turn_progress=ctx.turn_progress,
        non_human_wake=state.non_human_wake,
        explicit_visibility=ctx.require_agent().prompt.user_delivery_mode == "prefix",
        delivery_mode=ctx.require_agent().prompt.user_delivery_mode,
        source_message_id=message_id,
    )

    chunks: list[AIMessageChunk] = []

    messages = (
        _messages_with_current_visibility_rule(state.messages)
        if ctx.require_agent().prompt.user_delivery_mode == "prefix"
        else list(state.messages)
    )

    messages = await with_pending_deliveries(ctx, agent_id, messages)

    cancelled_cmd = await race_stream_vs_cancel(
        ctx,
        agent_id,
        stream_llm(
            llm,
            messages,
            chunks=chunks,
            handler=handler,
            agent=ctx.require_agent(),
            catalog=ctx.require_catalog(),
            monotonic=time.monotonic,
        ),
        handler,
    )
    if cancelled_cmd is not None:
        return cancelled_cmd

    if not chunks:
        empty_kw = {"meiva_non_human_wake": True} if state.non_human_wake else {}
        return Command[LlmGoto](
            update={"messages": [AIMessage(content="", additional_kwargs=empty_kw)]},
            goto=BEFORE_EXEC,
        )
    final_msg = assemble_final_message(chunks, catalog=ctx.require_catalog())
    final_msg.id = message_id

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
        now=ctx.require_clock().now(),
    )

    await capture_reply(ctx, agent_id, final_msg)

    cut_update = await due_chunk_update(
        state.compact,
        list(state.messages),
        final_msg,
        pool=ctx.ops_pool,
        agent_id=agent_id,
        agent=ctx.require_agent(),
        catalog=ctx.require_catalog(),
    )

    if not final_msg.tool_calls:
        note = await delivery_end_reminder(
            ctx,
            agent_id,
            state.user_delivery_baseline,
            correcting=_is_reply_correction(state.messages),
        )
        if note is not None:
            return Command[LlmGoto](
                update={"messages": [final_msg, note], "halted": False, **cut_update},
                goto=BEFORE_LLM,
            )
        if has_invalid_reply_visibility(final_msg):
            note = reply_visibility_note(ctx.require_clock().now())
            already_correcting = _is_reply_correction(state.messages)
            return Command[LlmGoto](
                update={"messages": [final_msg, note], "halted": already_correcting, **cut_update},
                goto=AFTER_EXEC if already_correcting else BEFORE_LLM,
            )
        logger.info("[halt] {body}", event="halt", body="no tool_call (idle)", agent_id=agent_id)
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
