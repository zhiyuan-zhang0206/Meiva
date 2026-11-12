# meiva:note
# Per-kind inbound dispatch for the claim node.
#
# Chat appends an envelope; compaction replaces history with its summary.
# Terminate and restart append acceptance markers and end the invocation so the
# host can flush the checkpoint before applying the command. Resurrect and fork
# append identity markers. Routing guards select lifecycle and compaction
# winners; unknown kinds fail fast.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeGuard

from langchain_core.messages import BaseMessage

from agent import state as _state
from agent.db import ClaimedInbound
from agent.graph.claim._chat_inbound import build_chat_inbound
from agent.graph.hooks import compact
from agent.graph.hooks.compact import (
    COMPACT_MAX_ATTEMPTS,
    conversation_messages,
)
from agent.graph.prompt import context_notes
from agent.hooks.compact_events import emit_compact_finished, emit_compact_started
from agent.messages import NoteTag, system_note_message
from agent.nodes import BEFORE_TURN, CLAIM, END
from agent.ownership.hosted import turn_crash_marked
from base.agents.context import MeivaContext
from base.agents.messages.envelope import sender_class
from base.agents.messages.inbound import InboundKind
from base.agents.messages.kwargs import MeivaMsgType
from base.agents.messages.lifecycle_text import by_who, restart_accepted_text
from base.agents.messages.reply_intent import delivery_disabled_entries
from base.api_contracts.agent_contract import Harness
from base.deploy.lifecycle.host_exit_message import HOST_RESTART_SOURCE
from base.events.live.projection import CompactionMode, CompactionStatus
from base.host.env.agent_slices import AgentSlices
from base.log import logger
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir

from ._routing import ROUTING_KINDS, ClaimGoto, Routing


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


@dataclass
class BatchState:
    """Mutable accumulator carried through one dispatch pass.

    Initialised with the inheritable channel values from AgentState; mutated
    in-place by the per-kind handlers during dispatch_batch().
    """

    new_msgs: list[BaseMessage] = field(default_factory=list[BaseMessage])
    next_goto: ClaimGoto = BEFORE_TURN
    compact_payload: tuple[str, str, str | None] | None = None
    """(summary_text, compact_kind, compact_run_id) — the run id pairs the
    live CompactStarted/CompactFinished events; None for the agent-authored
    compact_summary path (not a forced run, emits no ticking block)."""
    interrupted: bool = False
    restart_preserves_idle: bool = False
    restart_requested: bool = False
    committed_chat_ids: list[int] = field(default_factory=list[int])
    failure_notes: int = 0


def _ts_prefix(ctx: MeivaContext) -> str:
    """Leading `[ts] ` for lifecycle markers, or `` when agent-facing message
    timestamps are off (`settings.general.message_timestamps`)."""
    return (
        f"{ctx.require_clock().now_timestamp()} "
        if ctx.require_agent().read("general", "message_timestamps")
        else ""
    )


# meiva:note
# Ordinary claim and foreground steering share inbound message construction across chat, system
# notes, and heartbeats.
# meiva:link agent/messages/docs/message-format.md
def prepare_chat_inbound(ctx: MeivaContext, item: ClaimedInbound) -> list[BaseMessage]:
    return [
        build_chat_inbound(
            item,
            picked_up_at=ctx.require_clock().now(),
            envelope_inputs=ctx.timestamp_policy(),
            expand_command=ctx.require_commands().expand,
            gateway=lambda: ctx.gateway,
        )
    ]


def prepare_system_note_inbound(ctx: MeivaContext, item: ClaimedInbound) -> list[BaseMessage]:
    task_id = _task_id_from_system_note(item.payload, _system_note_tag(item.payload))
    now = ctx.require_clock().now()
    return [
        system_note_message(
            content=f"{_ts_prefix(ctx)}{item.content}",
            tag=_system_note_tag(item.payload),
            task_id=task_id,
            picked_up_at=now,
            created_at=item.created_at,
        )
    ]


def prepare_heartbeat_inbound(ctx: MeivaContext, item: ClaimedInbound) -> list[BaseMessage]:
    now = ctx.require_clock().now()
    return [
        system_note_message(
            content=f"{_ts_prefix(ctx)}{item.content}",
            tag=NoteTag.HEARTBEAT,
            picked_up_at=now,
            created_at=now,
        )
    ]


def prepare_steered_inbound(ctx: MeivaContext, item: ClaimedInbound) -> list[BaseMessage]:
    kind = InboundKind(item.kind)
    match kind:
        case InboundKind.CHAT:
            return prepare_chat_inbound(ctx, item)
        case InboundKind.SYSTEM_NOTE:
            return prepare_system_note_inbound(ctx, item)
        case InboundKind.HEARTBEAT:
            return prepare_heartbeat_inbound(ctx, item)
        case _:
            raise ValueError(f"Inbound kind is not steerable: {item.kind!r} (id={item.id})")


async def _handle_chat(
    ctx: MeivaContext,
    item: ClaimedInbound,
    st: BatchState,
) -> None:
    """CHAT inbound: wrap as HumanMessage, append to state, mark committed."""
    st.new_msgs.extend(prepare_chat_inbound(ctx, item))
    st.committed_chat_ids.append(item.id)


def _system_note_tag(payload: dict[str, object] | None) -> NoteTag:
    """The NoteTag for a system_note inbound, carried in its payload.

    `note_tag` is the one payload key this kind reads; missing, non-string,
    or unknown fails loud rather than silently defaulting — a wrong tag is a
    writer bug and renders as the wrong timeline chip.
    """
    if not payload:
        raise ValueError("system_note inbound requires a payload with 'note_tag'")
    raw = payload.get("note_tag")
    if not isinstance(raw, str):
        raise TypeError(f"system_note inbound 'note_tag' must be a NoteTag value, got {raw!r}")
    try:
        return NoteTag(raw)
    except ValueError as exc:
        raise ValueError(f"system_note inbound 'note_tag' {raw!r} is not a NoteTag value") from exc


def _task_id_from_system_note(payload: dict[str, object] | None, tag: NoteTag) -> int | None:
    """Return the timeline task link from a task note, if present."""
    if tag is not NoteTag.TASK or not payload or "task_id" not in payload:
        return None
    task_id = payload["task_id"]
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ValueError(
            f"system_note inbound 'task_id' must be a positive integer, got {task_id!r}"
        )
    return task_id


# meiva:note
# SYSTEM_NOTE inbound: render as a system note (system_marker), not chat.
async def _handle_system_note(
    ctx: MeivaContext,
    item: ClaimedInbound,
    st: BatchState,
) -> None:
    tag = _system_note_tag(item.payload)
    st.new_msgs.extend(prepare_system_note_inbound(ctx, item))
    if tag is NoteTag.LIFECYCLE_RESTART and st.next_goto == CLAIM:
        st.next_goto = BEFORE_TURN


def _close_superseded_compact(ctx: MeivaContext, agent_id: int, st: BatchState) -> None:
    """Close the tracked compact run sitting in the payload slot.

    A later compact outcome in this dispatch pass supersedes it: the slot
    holds one (summary, kind, run id) and the earlier run's result can never
    be applied once a newer one wins the batch. Its live block therefore gets
    its terminal `replaced` signal here — every started run must reach
    exactly one terminal state (success / failure / replaced).
    """
    prior = st.compact_payload
    if prior is not None and prior[2] is not None:
        emit_compact_finished(
            ctx.event_publisher,
            agent_id,
            prior[2],
            finished_at=ctx.require_clock().now(),
            status=CompactionStatus.REPLACED,
        )


async def _handle_compact_summary(
    ctx: MeivaContext,
    agent_id: int,
    item: ClaimedInbound,
    st: BatchState,
) -> None:
    """COMPACT_SUMMARY: agent-authored summary — used directly, no LLM run."""
    if ctx.harness is not Harness.MEIVA:
        logger.bind(agent_id=agent_id, harness=ctx.harness.value).warning(
            "dropping compact_summary for external harness {harness!r}: "
            "compaction is only supported on the native meiva harness",
            harness=ctx.harness.value,
        )
        return
    _close_superseded_compact(ctx, agent_id, st)
    st.compact_payload = (item.content, MeivaMsgType.COMPACT_SUMMARY.value, None)


# meiva:note
# COMPACT_REQUEST: run the compaction LLM to generate the summary. A request that cannot be satisfied ends in a failure note, not an exception (see `_record_compact_request_failure`). Dropped under an exit winner (the guard is in dispatch_batch via Routing.is_winner). An empty conversation is a normal state for a user-issued /compact (fresh agent), not a fault: consumed as a logged no-op. Past the no-op check the handler ends by overwriting the payload slot or recording a failure. A run already sitting in the slot can never be applied once a newer summary wins the batch, so its live block closes as `replaced` only after the new summary exists; a failed request supersedes nothing, and the earlier run is still applied. The compaction call can fail (provider error, empty output, a stalled or truncated stream); an exception that is none of those is a programming error and propagates after the live run is closed as a failure. Only what the auto path retries (an empty summary, a transient provider error) is asked again, since a stall or protocol failure repeats on the same request. When the request cannot be satisfied the batch carries on: the history stays as it is, a system note says why, and the chats claimed beside the request are still delivered. The request row is already consumed ('done'), so the requester asks again after reading the note.
async def _handle_compact_request(
    ctx: MeivaContext,
    agent_id: int,
    state: _state.AgentState,
    item: ClaimedInbound,
    st: BatchState,
) -> None:
    if ctx.harness is not Harness.MEIVA:
        logger.bind(agent_id=agent_id, harness=ctx.harness.value).warning(
            "dropping compact_request for external harness {harness!r}: "
            "compaction is only supported on the native meiva harness",
            harness=ctx.harness.value,
        )
        return
    assert ctx.llm is not None, "_handle_compact_request requires ctx.llm"  # noqa: S101
    if not conversation_messages(state.messages):
        logger.info(
            "[{label}] {body}",
            label="compact-request",
            body="request consumed as no-op: no conversation to compress",
            event="compact_request",
        )
        return
    compact_run_id = emit_compact_started(
        ctx.event_publisher,
        agent_id,
        compact_id=f"request-{item.id}",
        started_at=ctx.require_clock().now(),
        mode=CompactionMode.REQUEST,
    )
    failure: Exception | None = None
    summary = ""
    for attempt in range(1, COMPACT_MAX_ATTEMPTS + 1):
        try:
            summary = await compact.generate_summary(
                state.messages,
                ctx.llm,
                ctx.require_agent(),
                agent_id=agent_id,
                catalog=ctx.require_catalog(),
                now=ctx.require_clock().now,
            )
        except Exception as e:
            if not compact.compaction_failure(e):
                _close_failed_compact_run(ctx, agent_id, compact_run_id)
                raise
            failure = e
            if not compact.compaction_retryable(e):
                break
            logger.warning(
                "[{label}] {body}",
                label="compact-request",
                event="compact_request",
                body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: {compact.describe_failure(e)}",
            )
            continue
        failure = None
        break
    if failure is not None:
        _record_compact_request_failure(ctx, agent_id, compact_run_id, failure, st)
        return
    logger.info(
        "[{label}] {body}",
        label="compact-request",
        body=f"request summary {len(summary)} chars",
        event="compact_request",
    )
    _close_superseded_compact(ctx, agent_id, st)
    st.compact_payload = (summary, MeivaMsgType.COMPACT_REQUEST.value, compact_run_id)


def _close_failed_compact_run(ctx: MeivaContext, agent_id: int, compact_run_id: str | None) -> None:
    emit_compact_finished(
        ctx.event_publisher,
        agent_id,
        compact_run_id,
        finished_at=ctx.require_clock().now(),
        status=CompactionStatus.FAILURE,
    )


# meiva:note
# A compact request that could not produce a summary leaves the history as it was and says why
# in a system note, so the requester and the timeline both see the cause. The batch goes on: the
# chats claimed beside the request are delivered, and a batch with nothing else to answer idles.
def _record_compact_request_failure(
    ctx: MeivaContext,
    agent_id: int,
    compact_run_id: str | None,
    failure: Exception,
    st: BatchState,
) -> None:
    _close_failed_compact_run(ctx, agent_id, compact_run_id)
    reason = compact.describe_failure(failure)
    logger.warning(
        "[{label}] {body}",
        label="compact-request",
        event="compact_request",
        body=f"request not applied: {reason}",
    )
    st.new_msgs.append(compact.compact_failure_note(ctx, reason))
    st.failure_notes += 1


# meiva:note
# INTERRUPT: the control row of an external harness's interrupt; stop in-flight work, idle.
#
#     The history note and the message come as their own rows ahead of it, so it adds no marker.
async def _handle_interrupt(st: BatchState) -> None:
    st.interrupted = True


async def handle_heartbeat(
    ctx: MeivaContext,
    agent_id: int,
    item: ClaimedInbound,
    st: BatchState,
    state: _state.AgentState,
) -> None:
    """HEARTBEAT: check-in delivered as a system note.

    An open provider circuit consumes heartbeat without another model call;
    fresh chat and explicit controls keep their normal routing.
    """
    assert ctx.ops_pool is not None, "handle_heartbeat requires ctx.ops_pool"  # noqa: S101
    circuit = state.circuit
    if circuit.open:
        logger.warning(
            "heartbeat consumed while circuit breaker open (reason={reason}) — "
            "check-in note skipped, no LLM call",
            event="heartbeat_circuit_open",
            agent_id=agent_id,
            reason=circuit.reason,
        )
        st.next_goto = CLAIM
        return
    st.new_msgs.extend(prepare_heartbeat_inbound(ctx, item))


async def _handle_terminate(
    ctx: MeivaContext,
    item: ClaimedInbound,
    st: BatchState,
) -> None:
    """TERMINATE: append lifecycle marker, route to END.

    Only called when this terminate IS the routing winner (guard in
    dispatch_batch). Vetoed terminates never reach here.
    """
    now = ctx.require_clock().now()
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix(ctx)}Termination was accepted from {by_who(item.source)}",
            tag=NoteTag.LIFECYCLE_TERMINATE,
            picked_up_at=now,
            created_at=now,
        )
    )
    st.next_goto = END


# meiva:note
# Take a restart: end this invocation so the host can flush the checkpoint and apply the
# command, and settle what the agent is told.
#
# An agent that was idle stays idle (`restart_preserves_idle`) and reads the acceptance
# marker. An agent whose turn was unfinished resumes: the host's apply queues the one note
# that continues it (`restart_resume_note`), so no marker is added here. The two cases are
# told apart by `_turn_was_idle`, and a turn the exiting host's CLI died under (the corpse marker the
# host stamped for it) is unfinished although the new host's invocation starts idle. A restart is taken the same way on every harness: dropping one
# would leave the command claimed for the next incarnation to claim again, forever. The
# external harness's own restart (the agent's, `source='self'`, or the host's) reaches here only at
# a turn boundary, because the claim takes a lifecycle command first and the harness turn that
# was running has returned.
async def handle_restart(
    ctx: MeivaContext,
    state: _state.AgentState,
    item: ClaimedInbound,
    st: BatchState,
) -> None:
    st.next_goto = END
    st.restart_requested = True
    st.restart_preserves_idle = _turn_was_idle(
        state, item, crash_marked=await _host_restart_found_crash(ctx, item)
    )
    if st.restart_preserves_idle:
        now = ctx.require_clock().now()
        st.new_msgs.append(
            system_note_message(
                content=f"{_ts_prefix(ctx)}{restart_accepted_text(item.source, item.content or None)}",
                tag=NoteTag.LIFECYCLE_RESTART,
                picked_up_at=now,
                created_at=now,
            )
        )


# meiva:note
# Whether a restart finds the agent idle, with no unfinished turn to continue.
#
#     `halted` alone cannot say so. An interrupt pauses an agent by setting it, and so does a
#     model call or code run a restart interrupted, though that turn is unfinished:
#     `interrupted_by_restart` marks the latter. An agent with no history is idle. A
#     restart from the agent's own code (`meiva.self.restart()`) leaves its exec halted
#     with the turn unfinished, so it never counts as idle.
#
#     The host's own restart (`HOST_RESTART_SOURCE`) is written for a turn that was running when the
#     host exited. When that turn died before claiming it (an external harness's CLI is killed with
#     the host), the next host's invocation starts with `halted` set and would read an idle agent;
#     `crash_marked` is the durable record that the turn was cut short.
def _turn_was_idle(state: _state.AgentState, item: ClaimedInbound, *, crash_marked: bool) -> bool:
    halted = state.halted and not state.interrupted_by_restart
    return (halted or not state.messages) and item.source != "self" and not crash_marked


async def _host_restart_found_crash(ctx: MeivaContext, item: ClaimedInbound) -> bool:
    if item.source != HOST_RESTART_SOURCE or ctx.ops_pool is None:
        return False
    return await turn_crash_marked(ctx.ops_pool, item.agent_id)


async def _handle_resurrect(
    ctx: MeivaContext,
    item: ClaimedInbound,
    st: BatchState,
) -> None:
    """RESURRECT: append lifecycle marker, wake to BEFORE_LLM.

    Only called when no exit won (guard in dispatch_batch). When an exit
    beats the resurrect it is a consumed no-op — the agent is exiting,
    a 'resurrected' marker right before death would be noise.
    """
    now = ctx.require_clock().now()
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix(ctx)}You have been resurrected by {by_who(item.source)}",
            tag=NoteTag.LIFECYCLE_RESURRECT,
            picked_up_at=now,
            created_at=now,
        )
    )


# meiva:note
# The marker a fork appends after its inherited history. An external harness fork resumes a CLI
# session whose working directory was the source's: the CLI is told the new one by its own
# environment note, and the marker repeats it so the fact does not depend on that note. `workspace`
# is None for a native fork, whose conversation holds no directory.
# meiva:link base/lm/harness/docs/fork.md
def fork_marker_text(prefix: str, source: str, agent_id: int, workspace: Path | None) -> str:
    where = "" if workspace is None else f" Your working directory is now {workspace}."
    return (
        f"{prefix}You have been forked from {source}. "
        f"You are now a new, independent agent with id {agent_id} — the "
        f"conversation above is inherited from {source}, not your own "
        f"history. Identity, memory and skill notes in that history belong "
        f"to {source}; the notes below this marker are yours. "
        f"Continue as agent {agent_id}.{where}"
    )


# meiva:note
# A fork appends its lifecycle marker and its own context notes after the inherited history and never rewrites that history: the inherited messages stay byte-identical so the first fork of a checkpoint reuses the provider prefix cache its source warmed. The marker states that the identity, memory and skill notes above it belong to the source; the shared memory index is not re-grafted.
# meiva:link agent/graph/docs/context-notes/context-notes.md
async def _handle_fork(
    agent_id: int,
    item: ClaimedInbound,
    st: BatchState,
    slices: AgentSlices,
    extensions: ExtensionRegistry,
    ctx: MeivaContext,
) -> None:
    now = ctx.require_clock().now()
    workspace = (
        None
        if ctx.harness is Harness.MEIVA
        else workspace_dir(agent_id, home=ctx.require_meiva_home())
    )
    st.new_msgs.append(
        system_note_message(
            content=fork_marker_text(_ts_prefix(ctx), item.source, agent_id, workspace),
            tag=NoteTag.LIFECYCLE_FORK,
            picked_up_at=now,
            created_at=now,
        )
    )
    st.new_msgs.extend(context_notes.fork_notes(extensions, ctx))
    # Tail-graft skill additions (docs/decisions/2026-09-10-preset-in-config-overlay-
    # fork-cache): skills the fork's config added to
    # skills_to_inject_into_system_prompt (minus what the expand list already
    # grafts) arrive in the fork inbound's payload. Their full bodies append
    # AFTER the fork marker + on_fork notes — the tail, never the cached prefix.
    # Old fork rows carry no payload: skip.
    tail_skills = (item.payload or {}).get("tail_skills")
    if _is_object_list(tail_skills):
        from agent.graph.prompt.context_notes import fork_tail_skills_note

        note = fork_tail_skills_note(
            [s for s in tail_skills if isinstance(s, str)],
            (
                *slices.prompt.sdk_disable,
                *delivery_disabled_entries(slices.prompt.user_delivery_mode),
            ),
            home=ctx.require_meiva_home(),
            environ=ctx.require_environ(),
            created_at=now,
        )
        if note is not None:
            st.new_msgs.append(note)


async def _dispatch_item(
    ctx: MeivaContext,
    state: _state.AgentState,
    agent_id: int,
    item: ClaimedInbound,
    st: BatchState,
    latest_resurrect_id: int | None,
) -> None:
    """Route one claimed inbound to its handler by kind."""
    kind = item.kind
    if kind == InboundKind.CHAT:
        await _handle_chat(ctx, item, st)
    elif kind == InboundKind.SYSTEM_NOTE:
        await _handle_system_note(ctx, item, st)
    elif kind == InboundKind.COMPACT_SUMMARY:
        await _handle_compact_summary(ctx, agent_id, item, st)
    elif kind == InboundKind.COMPACT_REQUEST:
        await _handle_compact_request(ctx, agent_id, state, item, st)
    elif kind == InboundKind.INTERRUPT:
        await _handle_interrupt(st)
    elif kind == InboundKind.HEARTBEAT:
        await handle_heartbeat(ctx, agent_id, item, st, state)
    elif kind == InboundKind.TERMINATE:
        await _handle_terminate(ctx, item, st)
    elif kind == InboundKind.RESTART:
        await handle_restart(ctx, state, item, st)
    elif kind == InboundKind.RESURRECT:
        if item.id == latest_resurrect_id:
            await _handle_resurrect(ctx, item, st)
    elif kind == InboundKind.FORK:
        await _handle_fork(agent_id, item, st, ctx.require_agent(), ctx.plugin_registry(), ctx)
    else:
        raise ValueError(f"Unknown inbound kind: {kind!r} (id={item.id})")


async def dispatch_batch(
    ctx: MeivaContext,
    state: _state.AgentState,
    agent_id: int,
    batch: list[ClaimedInbound],
    routing: Routing,
    st: BatchState,
) -> None:
    """Dispatch every claimed inbound through its handler.

    Routing-guarded kinds (TERMINATE, RESTART, RESURRECT, COMPACT_REQUEST)
    are skipped when *routing.is_winner* returns False — one guard, not
    three scattered across the loop body. Multiple resurrect rows represent
    repeated failed wakes, so only the newest one renders a lifecycle marker.
    """
    latest_resurrect_id = max(
        (item.id for item in batch if item.kind == InboundKind.RESURRECT),
        default=None,
    )
    for item in batch:
        kind = item.kind
        if kind in ROUTING_KINDS and not routing.is_winner(item):
            continue
        await _dispatch_item(ctx, state, agent_id, item, st, latest_resurrect_id)


def _is_continuation_item(item: ClaimedInbound) -> bool:
    if item.kind in (
        InboundKind.COMPACT_SUMMARY,
        InboundKind.COMPACT_REQUEST,
        InboundKind.INTERRUPT,
    ):
        return True
    if item.kind == InboundKind.SYSTEM_NOTE and isinstance(item.payload, dict):
        return item.payload.get("note_tag") == NoteTag.LIFECYCLE_RESTART.value
    return False


def is_human_wake_item(item: ClaimedInbound) -> bool:
    return item.kind in (InboundKind.CHAT, InboundKind.RESURRECT) and (
        sender_class(item.source) == "user"
    )


def _is_dispatched_for_polarity(
    item: ClaimedInbound, routing: Routing, *, has_compact: bool
) -> bool:
    if item.kind in ROUTING_KINDS and not routing.is_winner(item):
        return False
    return not (has_compact and item.kind == InboundKind.CHAT)


# meiva:note
# Any human-sourced chat or manual resurrect (`user`) flips `non_human_wake`
# to `False` for the remainder of the run. Mid-run continuation/control mechanics
# (`COMPACT_SUMMARY`, `COMPACT_REQUEST`, `INTERRUPT`, interrupted-turn restart resume) and
# mid-run secondary arrivals preserve the active run's polarity; waking from idle on purely
# non-human inbounds sets `True`.
# meiva:link agent/docs/state.md
def resolve_non_human_wake(
    state: _state.AgentState,
    batch: Sequence[ClaimedInbound],
    routing: Routing,
    st: BatchState,
) -> bool:
    if st.restart_requested:
        return False if st.restart_preserves_idle else state.non_human_wake
    has_compact = st.compact_payload is not None
    dispatched = [
        item
        for item in batch
        if _is_dispatched_for_polarity(item, routing, has_compact=has_compact)
    ]
    if any(is_human_wake_item(item) for item in dispatched):
        return False
    if dispatched and all(_is_continuation_item(item) for item in dispatched):
        return state.non_human_wake
    return state.non_human_wake if (state.turn_active and not state.halted) else True
