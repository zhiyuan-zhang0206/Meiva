__description__ = "Auto-compact history when token count exceeds threshold"


from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.hooks.question_context import read_agent_open_questions
from agent.graph.interrupt import (
    ModelInterruptedError,
    interrupt_halt,
    interruptible_model,
    subscribe_interrupt,
)
from agent.graph.llm_errors import FatalLLMStreamError, FatalProviderError
from agent.graph.llm_stream.stream import stream_message
from agent.graph.prompt.compaction import compact_contract
from agent.graph.prompt.live import read_compact_doc
from agent.hooks import Hook
from agent.hooks.compact_anchor import SummaryText, closing_of, closing_request_of
from agent.hooks.compact_anchor import compose_summary_message as compose_summary_message
from agent.hooks.compact_events import emit_compact_finished, emit_compact_started
from agent.hooks.history_dump import dump_history, history_dump_note
from agent.hooks.understanding_chunks import await_snapshot, enqueue_closing_chunk
from agent.messages import (
    NoteTag,
    system_note_message,
)
from agent.nodes import CLAIM, INIT_CONTEXT, NodeName
from agent.state import AgentState, CompactState, ContextReset
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.history.closing_request import ClosingRequest
from base.agents.history.compact_boundary import mark_compact_boundary
from base.agents.messages.kwargs import MeivaMsgType, read_meiva_kwargs
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.questions.model import OpenQuestion
from base.agents.questions.render import reminder_text
from base.agents.to_user import DOCUMENT_FILENAME
from base.api_contracts.agent_contract import Harness
from base.clock import Timers
from base.events.live.projection import Cancelled, CompactDone, CompactionMode, CompactionStatus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import latest_input_tokens, resolve_context_budget
from base.lm.errors import ErrorClass, classify_error, is_retryable_provider_error
from base.lm.system_note import wrap_system_note
from base.log import logger
from base.packages.plugins import activation
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir
from base.telemetry.audit_events import prepare_event_log, record_audit_reported_async

COMPACTION_INSTRUCTION = """Context compaction checkpoint: the conversation above is about to be
replaced by the summary you write now. Produce it now as plain text — do not
write code or call any tool; anything other than text is discarded. Write it
exactly as the `meiva.self.compact` contract below specifies: first person, every
section filled, "(none)" only when one genuinely has nothing. Refer to others as
"the user" or "agent N". If a summary already appears above, rewrite it in place
— one flat, updated summary, never a summary nested inside a summary."""


COMPACT_MIN_SUMMARY_CHARS = 1000
COMPACT_MAX_ATTEMPTS = 3


class CompactionFailedError(RuntimeError):
    pass


class EmptyCompactionSummaryError(RuntimeError):
    pass


# meiva:note
# Which failed summary calls are worth another identical request: an empty summary and a transient provider error, as in a turn's llm node. A stall, a truncated or malformed stream and a permanent rejection repeat on the same request, so they end the attempt.
# meiva:link agent/graph/docs/context-window/replacement.md
def compaction_retryable(exc: Exception) -> bool:
    if isinstance(exc, (FatalLLMStreamError, FatalProviderError, CompactionFailedError)):
        return False
    return isinstance(exc, EmptyCompactionSummaryError) or is_retryable_provider_error(exc)


# meiva:note
# The failures of a summary call that belong to the model call: an empty summary, a fatal stream or provider failure, or an error the provider classifier recognizes (transient or permanent). Anything else is a programming error and is not turned into a failed compaction, as the llm node does not either.
# meiva:link agent/graph/docs/context-window/replacement.md
def compaction_failure(exc: Exception) -> bool:
    if isinstance(exc, (FatalLLMStreamError, FatalProviderError, EmptyCompactionSummaryError)):
        return True
    return classify_error(exc).error_class is not ErrorClass.UNKNOWN


# meiva:note
# The system note that tells an agent its compaction did not happen and why, so the failure is part of the history and the timeline rather than a live event only. The history is untouched.
def compact_failure_note(ctx: MeivaContext, reason: str) -> HumanMessage:
    clock = ctx.require_clock()
    now = clock.now()
    return system_note_message(
        content=(
            f"Compaction failed ({reason}). The history was not changed. "
            "Request compaction again after the cause is resolved."
        ),
        tag=NoteTag.COMPACT_FAILED,
        picked_up_at=now,
        created_at=now,
        inputs=ctx.timestamp_policy(),
    )


# meiva:note
# A failure named by its type: a bare timeout carries no message, and an empty reason hides what failed.
def describe_failure(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


# meiva:note
# Compaction stamps the newest pre-compaction checkpoint as the segment anchor rather than deleting checkpoints. A missing pool is a no-op; stamp failures are reported and do not abort compaction. A closing understanding chunk is anchored to the successful stamp.
# meiva:link agent/graph/docs/context-window/replacement.md
async def stamp_compact_boundary(
    pool: AsyncConnectionPool | None,
    agent_id: int,
    state: AgentState | None = None,
    closing: ClosingRequest | None = None,
    *,
    understanding_enabled: bool,
    timers: Timers,
) -> None:
    if pool is None:
        return
    boundary: str | None = None
    await await_snapshot(
        pool, state, agent_id, understanding_enabled=understanding_enabled, timers=timers
    )
    try:
        boundary = await mark_compact_boundary(pool, str(agent_id), closing=closing)
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="compact-boundary",
            event="compact_boundary_stamp",
            body=f"compact boundary stamp failed for agent {agent_id}: {exc!r}",
        )
    if state is not None:
        await enqueue_closing_chunk(
            state.compact,
            list(state.messages),
            pool=pool,
            agent_id=agent_id,
            boundary=boundary,
            understanding_enabled=understanding_enabled,
        )


def conversation_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    has_system = bool(messages) and isinstance(messages[0], SystemMessage)
    return messages[1:] if has_system else messages


# meiva:note
# compaction_completed describes an applied replacement, not a request. It counts one completion, measures conversation characters excluding the standing head, omits a ratio for empty history, and attributes reminder_shown only to the version being replaced.
# meiva:link agent/graph/docs/context-window/preparation.md
def emit_compaction_monitoring(
    messages: list[AnyMessage],
    summary: str,
    *,
    agent_id: int,
    compact_kind: str,
    compact: CompactState,
) -> None:
    history_chars = sum(
        len(message.content) if isinstance(message.content, str) else len(str(message.content))
        for message in conversation_messages(messages)
    )
    summary_chars = len(summary)
    summary_history_ratio = summary_chars / history_chars if history_chars else None
    logger.info(
        "compaction completed: {history_chars} history chars -> {summary_chars} summary chars",
        event="compaction_completed",
        agent_id=agent_id,
        compact_kind=compact_kind,
        reminder_shown=(
            compact.reminder_shown and compact.reminder_seen_version == compact.version
        ),
        compactions=1,
        history_chars=history_chars,
        summary_chars=summary_chars,
        summary_history_ratio=summary_history_ratio,
    )


# meiva:note
# Generated compaction appends the shared meiva.self.compact instructions to the complete existing conversation prefix. Empty conversations fail before invocation and empty returned text fails validation; tool-only responses are not summaries.
# meiva:link agent/graph/docs/context-window/replacement.md
async def generate_summary(
    messages: list[AnyMessage],
    llm: BaseChatModel,
    slices: AgentSlices,
    *,
    inputs: TimestampPolicy,
    agent_id: int,
    catalog: ModelCatalog,
    now: Callable[[], datetime],
) -> SummaryText:
    has_system = bool(messages) and isinstance(messages[0], SystemMessage)
    system_head = messages[:1] if has_system else []
    content_msgs = messages[1:] if has_system else messages

    if not content_msgs:
        raise ValueError("conversation is empty, nothing to compress")

    # meiva:note
    # The checkpoint instruction and the contract it points at are one system note, so the
    # model reads where the order ends.
    # meiva:link agent/messages/docs/message-format.md
    instruction = HumanMessage(
        content=wrap_system_note(
            f"{inputs.prefix()}{COMPACTION_INSTRUCTION}\nmeiva.self.compact contract:\n"
            f"{compact_contract(read_compact_doc())}"
        )
    )
    compaction_input = [*system_head, *content_msgs, instruction]
    response = await stream_message(llm, compaction_input, agent=slices, catalog=catalog)
    model = slices.brain.llm_model
    if isinstance(model, str) and model:
        from base.lm.usage import UsageOptions, log_usage_from_message

        # meiva:note
        # The compaction call is an agent's own spend, and the row carries that agent: this
        # process serves several agents, so the ambient identity is None here and a row that
        # falls back to it is `unattributed` (task #6444).
        log_usage_from_message(
            response,
            model=model,
            usage_kind="agent",
            options=UsageOptions(for_agent_id=agent_id, source="compaction", stamp_message=True),
            catalog=catalog,
            now=now(),
        )
    summary = response.text
    closing = closing_request_of(response, instruction)
    if not summary.strip():
        raise EmptyCompactionSummaryError(
            f"Compaction LLM returned no text content"
            f" (summarizing {len(content_msgs)} messages,"
            f" response content type {type(response.content).__name__});"
            f" cannot replace history with an empty summary"
        )
    return SummaryText(summary, closing)


def _estimate_tokens(messages: list[AnyMessage]) -> int:
    total_chars = sum(
        len(m.content) if isinstance(m.content, str) else len(str(m.content)) for m in messages
    )
    return total_chars // 4


# meiva:link agent/graph/docs/context-window/preparation.md
def _context_occupancy(messages: list[AnyMessage]) -> int:
    tokens = latest_input_tokens(messages)
    return tokens if tokens is not None else _estimate_tokens(messages)


# meiva:link agent/graph/docs/context-window/preparation.md
def _summary_awaits_reply(messages: list[AnyMessage]) -> bool:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return False
        if isinstance(message, HumanMessage) and read_meiva_kwargs(message).get(
            "meiva_msg_type"
        ) in (
            MeivaMsgType.COMPACT_SUMMARY.value,
            MeivaMsgType.COMPACT_REQUEST.value,
        ):
            return True
    return False


def auto_compact_will_fire(state: AgentState, agent: AgentSlices, *, catalog: ModelCatalog) -> bool:
    if _summary_awaits_reply(state.messages):
        return False
    budget = resolve_context_budget(agent.brain.llm_model, agent.overrides, catalog=catalog)
    if _context_occupancy(state.messages) <= budget.hard_compact_tokens:
        return False
    return bool(conversation_messages(state.messages))


# meiva:link agent/graph/docs/context-window/replacement.md
async def auto_compact_summary(
    messages: list[AnyMessage],
    llm: BaseChatModel,
    content_count: int,
    slices: AgentSlices,
    *,
    inputs: TimestampPolicy,
    agent_id: int,
    catalog: ModelCatalog,
    now: Callable[[], datetime],
) -> str:
    summary: str = ""
    last_error: Exception | None = None
    for attempt in range(1, COMPACT_MAX_ATTEMPTS + 1):
        try:
            summary = await generate_summary(
                messages, llm, slices, agent_id=agent_id, catalog=catalog, now=now, inputs=inputs
            )
        except Exception as e:
            if not compaction_retryable(e):
                raise
            last_error = e
            logger.warning(
                "[{label}] {body}",
                label="auto-compact",
                event="auto_compact",
                body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: {describe_failure(e)}; retrying",
            )
            continue
        logger.info(
            "[{label}] {body}",
            label="auto-compact",
            event="auto_compact",
            body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: summary {len(summary)} chars",
        )
        if len(summary) >= COMPACT_MIN_SUMMARY_CHARS:
            break
        last_error = None
    else:
        detail = f"error: {describe_failure(last_error)}" if last_error else f"{len(summary)} chars"
        raise CompactionFailedError(
            f"Compaction produced no usable summary across {COMPACT_MAX_ATTEMPTS}"
            f" attempts (last: {detail}); the model is not following the"
            f" compaction template — refusing to overwrite {content_count} messages"
            f" with a non-summary"
        ) from last_error

    return summary


# meiva:link agent/graph/docs/context-window/replacement.md
async def auto_compact_for_llm(
    state: AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> dict[str, Any] | None:
    if _summary_awaits_reply(state.messages):
        return None
    occupancy = _context_occupancy(state.messages)
    agent = runtime.context.require_agent()
    catalog = runtime.context.require_catalog()
    budget = resolve_context_budget(agent.brain.llm_model, agent.overrides, catalog=catalog)
    if occupancy <= budget.hard_compact_tokens:
        return None
    content_msgs = conversation_messages(state.messages)
    if not content_msgs:
        logger.info(
            "[{label}] {body}",
            label="auto-compact",
            event="auto_compact",
            body=f"skip: nothing to compress (no conversation messages), tokens≈{occupancy}",
        )
        return None
    llm = runtime.context.llm
    if llm is None:
        raise RuntimeError("auto_compact_for_llm requires ctx.llm")
    logger.info(
        "[{label}] {body}",
        label="auto-compact",
        event="auto_compact",
        body=f"start: tokens≈{occupancy}, compressing {len(content_msgs)} messages",
    )
    agent_id = agent_id_from_config(config)
    publisher = runtime.context.event_publisher
    started_at = runtime.context.require_clock().now()
    compact_run_id = emit_compact_started(
        publisher,
        agent_id,
        compact_id=f"auto-{state.compact.version}-{started_at.isoformat()}",
        started_at=started_at,
        mode=CompactionMode.AUTO,
    )

    try:
        async with subscribe_interrupt(
            runtime.context.ops_pool,
            agent_id,
            resources=runtime.context.hosted_resources,
        ) as interrupted:
            summary = await interruptible_model(
                auto_compact_summary(
                    list(state.messages),
                    llm,
                    len(content_msgs),
                    runtime.context.require_agent(),
                    agent_id=agent_id,
                    catalog=catalog,
                    now=runtime.context.require_clock().now,
                    inputs=runtime.context.timestamp_policy(),
                ),
                interrupted,
            )
    except ModelInterruptedError as interrupt:
        emit_compact_finished(
            publisher,
            agent_id,
            compact_run_id,
            finished_at=runtime.context.require_clock().now(),
            status=CompactionStatus.REPLACED,
        )
        if publisher is not None:
            publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())
        return {**interrupt_halt(interrupt.reason), "goto": CLAIM}
    except BaseException:
        emit_compact_finished(
            publisher,
            agent_id,
            compact_run_id,
            finished_at=runtime.context.require_clock().now(),
            status=CompactionStatus.FAILURE,
        )
        raise

    logger.info(
        "[{label}] {body}",
        label="auto-compact",
        event="auto_compact",
        body=f"done: summary {len(summary)} chars",
    )

    emit_compaction_monitoring(
        state.messages,
        summary,
        agent_id=agent_id,
        compact_kind="auto",
        compact=state.compact,
    )
    pool = runtime.context.ops_pool

    if pool is not None:
        await record_audit_reported_async(
            pool,
            prepare_event_log(
                event_type="compact",
                agent_id=agent_id,
                source="system",
                payload={"compact_kind": "auto", "length": len(summary)},
            ),
        )

    if publisher is not None:
        publisher.emit(CompactDone(agent_id=agent_id).model_dump_json())

    now = runtime.context.require_clock().now()
    dump_path = dump_history(
        state.messages,
        workspace_dir(agent_id, home=runtime.context.require_meiva_home()),
        runtime.context.require_agent().history_dump,
        now=now,
    )
    summary_kwargs: dict[str, Any] = {
        "additional_kwargs": {
            "meiva_msg_type": MeivaMsgType.COMPACT_SUMMARY.value,
            "meiva_created_at": now.isoformat(),
        },
    }
    if compact_run_id is not None:
        summary_kwargs["additional_kwargs"]["meiva_compact_id"] = compact_run_id
    transition = build_compact_transition(
        summary,
        resume=CLAIM,
        extra_msgs=(
            [
                history_dump_note(
                    dump_path, created_at=now, inputs=runtime.context.timestamp_policy()
                )
            ]
            if dump_path is not None
            else None
        ),
        summary_kwargs=summary_kwargs,
        inputs=runtime.context.timestamp_policy(),
    )
    await stamp_compact_boundary(
        runtime.context.ops_pool,
        agent_id,
        state,
        closing_of(summary),
        understanding_enabled=runtime.context.require_agent().read(
            "agent", "understanding_enabled"
        ),
        timers=runtime.context.require_clock(),
    )
    transition["compact"] = state.compact.next_segment()
    emit_compact_finished(
        publisher, agent_id, compact_run_id, finished_at=now, status=CompactionStatus.SUCCESS
    )
    return transition


# meiva:link agent/graph/docs/context-window/replacement.md
def build_compact_transition(
    summary: str,
    *,
    inputs: TimestampPolicy,
    resume: NodeName,
    extra_msgs: list[AnyMessage] | None = None,
    summary_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary_msg_kwargs = summary_kwargs or {}
    tail_msgs: list[AnyMessage] = [
        HumanMessage(
            content=compose_summary_message(summary, inputs=inputs),
            **summary_msg_kwargs,
        ),
    ]
    if extra_msgs:
        tail_msgs.extend(extra_msgs)
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
        "context_reset": ContextReset(
            tail=tail_msgs,
            resume=resume,
        ),
        "goto": INIT_CONTEXT,
    }


COMPACT_REMINDER_NOTE = (
    "Your working context has reached the soft compaction threshold. "
    "Wind down this context now: finish only the immediate safe step, "
    "persist new durable information, and complete the preparation below. "
    "Then call meiva.self.compact(summary) before starting further substantive work. "
    "Keep preparation brief; do not start a new investigation or wait for the "
    "hard limit, where automatic compaction cannot run file-writing tools. "
    "Write the summary as its docstring specifies. "
    "Read meiva.help(meiva.self.compact) if you need the contract."
)


# meiva:link agent/graph/docs/context-window/preparation.md
def _reminder_content(
    agent: AgentSlices,
    *,
    catalog: ModelCatalog,
    extensions: ExtensionRegistry,
    open_questions: Sequence[OpenQuestion],
) -> str:
    parts = [COMPACT_REMINDER_NOTE]
    if agent.read("agent", "workspace_in_system_prompt"):
        parts.append(
            f"Update your workspace's `{DOCUMENT_FILENAME}` shared with the user if it is stale: "
            "preserve current results, decisions, open questions and next steps; "
            "remove outdated status, within the panel's soft word limit — links and images "
            "render for the user, so detail can live in linked files. Edit in place, "
            "without a separate notification."
        )
    questions = reminder_text(open_questions)
    if questions:
        parts.append(questions)
    for plugin, section in extensions.compact_reminder_sections():
        contribution = section(agent, catalog=catalog)
        if not isinstance(contribution, str):
            raise TypeError(f"{plugin}.{section.__name__} must return reminder text")
        if contribution.strip():
            parts.append(f"[{plugin}]\n{contribution.strip()}")
            activation.record(
                plugin,
                "compactReminderSections",
                section.__name__,
                model=agent.brain.llm_model,
                detail=f"chars={len(contribution)}",
            )
    return "\n\n".join(parts)


# meiva:note
# Whether the wind-down reminder is owed now: the context is over the soft threshold, holds a
# conversation, and the reminder was not already shown for this compaction version.
# meiva:link agent/graph/docs/context-window/preparation.md
def reminder_due(state: AgentState, agent: AgentSlices, *, catalog: ModelCatalog) -> bool:
    occupancy = _context_occupancy(state.messages)
    soft = resolve_context_budget(
        agent.brain.llm_model, agent.overrides, catalog=catalog
    ).soft_compact_tokens
    if occupancy <= soft or not conversation_messages(state.messages):
        return False
    compact = state.compact
    shown = compact.reminder_shown and compact.reminder_seen_version >= compact.version
    return not shown


# meiva:link agent/graph/docs/context-window/preparation.md
def compact_reminder_update(
    state: AgentState,
    agent: AgentSlices,
    *,
    inputs: TimestampPolicy,
    catalog: ModelCatalog,
    extensions: ExtensionRegistry,
    open_questions: Sequence[OpenQuestion],
    now: datetime,
) -> dict[str, Any] | None:
    if not reminder_due(state, agent, catalog=catalog):
        return None
    compact: CompactState = state.compact
    seen = max(compact.reminder_seen_version, compact.version)
    logger.info(
        "[{label}] {body}",
        label="compact-reminder",
        event="compact_reminder",
        body=f"injecting wind-down note, tokens≈{_context_occupancy(state.messages)}",
    )
    note = system_note_message(
        content=_reminder_content(
            agent, catalog=catalog, extensions=extensions, open_questions=open_questions
        ),
        tag=NoteTag.COMPACT_REMINDER,
        picked_up_at=now,
        created_at=now,
        inputs=inputs,
    )
    return {
        "messages": [note],
        "compact": compact.model_copy(
            update={"reminder_shown": True, "reminder_seen_version": seen}
        ),
    }


class _CompactReminderHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        if runtime.context.harness is not Harness.MEIVA:
            return None
        agent = runtime.context.require_agent()
        catalog = runtime.context.require_catalog()
        if _summary_awaits_reply(state.messages) or auto_compact_will_fire(
            state, agent, catalog=catalog
        ):
            return None
        if not reminder_due(state, agent, catalog=catalog):
            return None
        owner = runtime.context.identity.agent_id if runtime.context.identity else None
        return compact_reminder_update(
            state,
            agent,
            catalog=catalog,
            extensions=runtime.context.plugin_registry(),
            open_questions=await read_agent_open_questions(runtime.context, owner),
            now=runtime.context.require_clock().now(),
            inputs=runtime.context.timestamp_policy(),
        )


compact_reminder = _CompactReminderHook()
