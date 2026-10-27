"""Durable native checkpoint receipts for automatic session endings."""

import asyncio
from collections.abc import Awaitable, Collection
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from psycopg.types.json import Jsonb

from agent.messages import system_note_message
from agent.state import BaseAgentState
from agent.takeover.notes import (
    NoteDraft,
    NoteStamp,
    SessionRef,
    handoff_draft,
    handoff_note_id,
    handoff_receipt,
    handoff_summary,
    introduction_draft,
    start_drafts,
)
from base.agents.context import MeivaContext
from base.agents.impersonation import OPEN, lock_lease, require_native
from base.agents.impersonation.history import export_handoff, metadata
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.messages.kwargs import NoteTag
from base.agents.messages.timestamp_policy import TimestampPolicy, TimestampSource
from base.clock import Clock
from base.db.protocols import DatabaseLike
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation

AgentGraph = CompiledStateGraph[BaseAgentState, MeivaContext, BaseAgentState, BaseAgentState]


async def flush_checkpoint(checkpointer: object, agent_id: int) -> None:
    """Flush the optional buffered saver without probing dynamic attributes."""
    attributes: dict[str, object] = getattr(checkpointer, "__dict__", {})
    flush = attributes.get("_meiva_nstep_flush")
    if callable(flush):
        pending = flush(str(agent_id))
        if isinstance(pending, Awaitable):
            await pending


# meiva:note
# LangGraph types the compiled graph's checkpointer with unknown generics, so the flush receives
# it through the structural `object` that `flush_checkpoint` accepts.
async def flush_graph_checkpoint(graph: AgentGraph, agent_id: int) -> None:
    await flush_checkpoint(graph.checkpointer, agent_id)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def note_stamp(clock: Clock, *, timestamps: bool) -> NoteStamp:
    created_at = clock.now()
    return NoteStamp(created_at, f"{clock.format_timestamp(created_at)} " if timestamps else "")


def note_stamp_of(notes: TimestampPolicy) -> NoteStamp:
    created_at = notes.require_now()
    return NoteStamp(created_at, f"{notes.format(created_at)} " if notes.timestamps_enabled else "")


def _render(draft: NoteDraft, session: dict[str, Any] | None = None) -> HumanMessage:
    note = system_note_message(
        content=draft.text,
        tag=NoteTag.IMPERSONATION,
        picked_up_at=draft.created_at,
        created_at=draft.created_at,
    )
    note.id = draft.note_id
    if draft.anchors_session and session is not None:
        note.additional_kwargs["meiva_impersonation"] = metadata(session).model_dump()
    return note


def introduction_note(stamp: NoteStamp) -> HumanMessage:
    """Standing native context, introduced only once a takeover is encountered."""
    return _render(introduction_draft(stamp))


def start_update(
    session: dict[str, Any],
    *,
    introduced: bool,
    stamp: NoteStamp,
    existing_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Commit the introduction receipt and the takeover anchor together."""
    drafts = start_drafts(
        SessionRef.of(session), stamp, introduced=introduced, existing_ids=existing_ids
    )
    return {
        "messages": [_render(draft, session) for draft in drafts],
        "impersonation_introduced": True,
    }


async def ensure_start_marker(
    graph: AgentGraph, session: dict[str, Any], *, notes: TimestampPolicy
) -> None:
    """Repair a crash between accepting the request and saving its timeline anchor."""
    config: RunnableConfig = {"configurable": {"thread_id": str(session["agent_id"])}}
    snapshot = await graph.aget_state(config)
    introduced = snapshot.values.get("impersonation_introduced", False)
    update = start_update(
        session,
        introduced=introduced,
        stamp=note_stamp_of(notes),
        existing_ids={message.id for message in snapshot.values.get("messages", [])},
    )
    if update["messages"] or not introduced:
        await graph.aupdate_state(config, update)
    await flush_graph_checkpoint(graph, session["agent_id"])


def resume_note_pending(state: BaseAgentState) -> bool:
    """Whether the newest message is an end-of-session note awaiting its first turn.

    `deliver_handoff` appends the note as the first resumed input. It is a system
    note, so a window that never carried a real exchange still reads as "no
    conversation" and the claim would idle out with the note unprocessed. Until
    the agent takes its first turn after delivery (which appends newer messages),
    the trailing note itself marks the pending resume.
    """
    receipt = state.impersonation_handoff_id
    messages = state.messages
    if receipt is None or not messages:
        return False
    return getattr(messages[-1], "id", None) == handoff_note_id(receipt)


# meiva:note
# Whether a settled handoff left an unprocessed trailing resume note.
async def handoff_resume_pending(graph: AgentGraph, agent_id: int) -> bool:
    snapshot = await graph.aget_state({"configurable": {"thread_id": str(agent_id)}})
    state = BaseAgentState.model_validate(snapshot.values)
    return not state.halted and resume_note_pending(state)


def save_document(
    db: DatabaseLike, session: dict[str, Any], incarnation: RuntimeIncarnation, *, workspace: Path
) -> tuple[str, str]:
    with db.write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, session["id"])
        if lease["status"] in OPEN:
            raise RuntimeError("Cannot hand back an open impersonation session")
        document, path = export_handoff(lease, conn, workspace=workspace)
        conn.execute(
            "UPDATE agent_impersonations SET handoff_document=%s,handoff_path=%s WHERE id=%s",
            (Jsonb(document), path, lease["id"]),
        )
    summary = handoff_summary(
        lease["summary"], status=lease["status"], rejection_reason=lease["rejection_reason"]
    )
    return summary, path


def record_handoff_receipt(
    db: DatabaseLike, session: dict[str, Any], incarnation: RuntimeIncarnation
) -> None:
    with db.write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, session["id"])
        # These messages have now been delivered in the durable end-of-session note
        # and record file.
        # Their immutable bodies survive independently of processing status.
        conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE agent_id=%s AND status='pending' "
            "AND id IN (SELECT (payload->>'inbound_id')::bigint "
            "FROM agent_impersonation_entries WHERE lease_id=%s AND kind='message' "
            "AND payload->>'direction'='in')",
            (lease["agent_id"], lease["id"]),
        )
        conn.execute(
            "UPDATE agent_impersonations SET handoff_applied_at=clock_timestamp() WHERE id=%s",
            (lease["id"],),
        )


async def deliver_handoff(
    graph: AgentGraph,
    db: DatabaseLike,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    *,
    timestamps: TimestampSource,
    workspace: Path,
    reason: str | None = None,
) -> None:
    """Save file, append the first resumed input, flush checkpoint, then receipt.

    The native gate remains closed throughout. A crash before the checkpoint
    repeats the same message id; a crash after it only repeats the DB receipt.
    Ordinary queued input cannot be claimed until the receipt releases the gate.
    The receipt is followed by a best-effort wake: the note's first turn must run
    even when nothing else is queued (claim also resumes on the trailing note, so
    an empty queue is not an idle verdict).

    Event accounting never delays the native checkpoint or its receipt: a log-native
    lease is complete in the database or keeps its documented pending semantics until
    its last open source seals.

    ``reason`` is the death cause of a supervisor-aborted session (task #3998);
    when present, the note names it right after the session-end sentence.
    """
    # Keep the truthful pending record while the durable native handoff proceeds.
    summary, path = await asyncio.to_thread(
        save_document, db, session, incarnation, workspace=workspace
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(incarnation.agent_id)}}
    snapshot = await graph.aget_state(config)
    ref = SessionRef.of(session)
    receipt = handoff_receipt(ref)
    if snapshot.values.get("impersonation_handoff_id") != receipt:
        draft = handoff_draft(
            ref, note_stamp_of(timestamps.policy()), summary=summary, path=path, reason=reason
        )
        await graph.aupdate_state(
            config,
            {
                "messages": [_render(draft)],
                "impersonation_handoff_id": receipt,
                "halted": False,
                "turn_idle": False,
                "turn_active": True,
            },
        )
    await flush_graph_checkpoint(graph, incarnation.agent_id)
    await asyncio.to_thread(record_handoff_receipt, db, session, incarnation)
    # The receipt opens the claim gate; this wake drives the note's first turn when
    # nothing else is queued (and re-drives it if this turn ends before that
    # invocation). Best-effort: the host latches wakes per agent.
    publish_inbound_wake(db, bus, session["agent_id"], "impersonation")
