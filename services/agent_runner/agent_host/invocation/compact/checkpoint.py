"""Fresh persisted compact application proof; graph return and summary success are insufficient."""

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.graph.hooks.compact import compose_summary_message, retained_tail
from agent.state import CompactState, ContextReset
from base.agents.compaction.execution import CompactCommand
from base.agents.compaction.models import CompactHeldError, CompactMarker
from base.agents.history.checkpoint_postgres_walks import HistoryAsyncPostgresSaver
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.messages.kwargs import MeivaMsgType


def cold_reader(saver: AsyncPostgresSaver) -> AsyncPostgresSaver:
    """A fresh persisted delta reconstruction, without runtime/pending-write caches."""
    reader = HistoryAsyncPostgresSaver(saver.conn, serde=saver.serde)
    wrap_saver_reads_with_delta_reconstruction(reader)
    return reader


def marker_for(command: CompactCommand) -> CompactMarker:
    if command.result is None or command.execution is None or command.attempt_id is None:
        raise CompactHeldError("compact marker requires original execution and durable result")
    return CompactMarker(
        acceptance=command.acceptance,
        execution=command.execution,
        attempt_id=command.attempt_id,
        result_digest=command.result.digest(),
        segment_version=command.acceptance.target.segment_version + 1,
    )


async def cold_application(saver: AsyncPostgresSaver, command: CompactCommand) -> str | None:
    """Read committed materialized channels, excluding buffer and pending writes."""
    reader = cold_reader(saver)
    snapshot = await reader.aget_tuple(
        {"configurable": {"thread_id": str(command.acceptance.target.source.agent_id)}}
    )
    if snapshot is None:
        return None
    values = snapshot.checkpoint["channel_values"]
    if values.get("native_compact") is None:
        return None
    marker = CompactMarker.model_validate(values["native_compact"])
    if marker != marker_for(command):
        return None
    source = await reader.aget_tuple(
        {
            "configurable": {
                "thread_id": str(command.acceptance.target.source.agent_id),
                "checkpoint_id": command.acceptance.target.checkpoint_id,
            }
        }
    )
    if source is None:
        return None
    if not _materialized(
        values, command, marker, source.checkpoint["channel_values"].get("messages", [])
    ):
        return None
    return snapshot.checkpoint["id"]


def _materialized(
    values: dict[str, Any],
    command: CompactCommand,
    marker: CompactMarker,
    source_messages: list[Any],
) -> bool:
    reset = ContextReset.model_validate(values.get("context_reset", {}))
    compact = CompactState.model_validate(values.get("compact", {}))
    if (
        values.get("halted") is not True
        or values.get("turn_idle") is not True
        or values.get("turn_active") is not False
        or reset.tail
        or compact.version != marker.segment_version
        or NativeWorkTarget.model_validate(values.get("native_work")) != marker.execution
    ):
        return False
    return _replacement_head(values.get("messages", []), command, source_messages)


def _is_summary(message: Any, command: CompactCommand) -> bool:
    result = command.result
    return (
        result is not None
        and isinstance(message, HumanMessage)
        and message.id == str(result.message_id)
        and message.content == compose_summary_message(result.summary)
        and message.additional_kwargs.get("meiva_compact_id") == str(command.acceptance.command_id)
    )


def _is_replacement_note(message: Any) -> bool:
    return (
        isinstance(message, HumanMessage)
        and message.additional_kwargs.get("meiva_msg_type") != MeivaMsgType.INBOUND.value
    )


def _replacement_head(
    messages: list[Any], command: CompactCommand, source_messages: list[Any]
) -> bool:
    result = command.result
    if result is None or len(messages) < 2 or not isinstance(messages[0], SystemMessage):
        return False
    summary_index = next(
        (index for index, message in enumerate(messages) if _is_summary(message, command)),
        None,
    )
    if summary_index is None or not all(
        _is_replacement_note(message) for message in messages[1:summary_index]
    ):
        return False
    old_ids = {message.id for message in source_messages}
    if any(message.id in old_ids for message in messages[: summary_index + 1]):
        return False
    return verbatim_suffix(messages[summary_index + 1 :], source_messages)


def verbatim_suffix(window: list[Any], source_messages: list[Any]) -> bool:
    if not window:
        return True
    filtered = retained_tail(source_messages, len(source_messages))
    expected = filtered[-len(window) :]
    return [_comparable(message) for message in expected] == [
        _comparable(message) for message in window
    ]


def _comparable(message: Any) -> tuple[str, str, str, tuple[str, ...]]:
    calls = message.tool_calls if isinstance(message, AIMessage) else ()
    return (
        type(message).__name__,
        str(message.id),
        str(message.content),
        tuple(str(call.get("id")) for call in calls),
    )
