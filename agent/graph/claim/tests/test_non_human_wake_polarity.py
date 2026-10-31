from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.db import ClaimedInbound
from agent.graph.claim._dispatch import BatchState, resolve_non_human_wake
from agent.graph.claim._routing import Routing
from agent.graph.claim.harness_turn import HarnessTurnProjector
from agent.graph.claim.node import claim_node_impl, settle_claimed_batch
from agent.graph.claim.tests.claim_support import make_runtime, thread_config
from agent.graph.claim.turn_stamp import persist_last_active
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState
from base.agents.observation.turn_progress import TurnProgress
from base.agents.tests.units import spawn_agent
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.events.live.tests.live_capture import LiveEvents
from base.lm.catalog import ModelCatalog
from base.lm.harness import AssistantText, HarnessTurn, TextDelta, ToolUse, Usage

START = datetime(2026, 10, 25, 1, 2, 3, tzinfo=UTC)


def _routing() -> Routing:
    return Routing(exit_kind=None, has_revive=False, terminate_vetoed_by_pending=False)


def _inbound(
    inbound_id: int,
    *,
    kind: str = "chat",
    source: str = "user",
    payload: dict[str, object] | None = None,
) -> ClaimedInbound:
    return ClaimedInbound(
        id=inbound_id,
        agent_id=7,
        content="msg",
        kind=kind,
        source=source,
        payload=payload,
    )


@pytest.mark.parametrize("human_source", ["user", "ui:page:board"])
def test_human_sources_resolve_human_polarity_from_idle_or_background_run(
    human_source: str,
) -> None:
    idle_state = AgentState(halted=True, turn_active=False, non_human_wake=False)
    assert (
        resolve_non_human_wake(
            idle_state, [_inbound(1, source=human_source)], _routing(), BatchState()
        )
        is False
    )

    mixed_batch = [_inbound(1, source="agent:42"), _inbound(2, source=human_source)]
    bg_mid_run = AgentState(halted=False, turn_active=True, non_human_wake=True)
    assert resolve_non_human_wake(bg_mid_run, mixed_batch, _routing(), BatchState()) is False


@pytest.mark.parametrize(
    ("kind", "source", "payload"),
    [
        ("chat", "agent:42", None),
        ("chat", "external_agent:claude_code", None),
        ("chat", "watcher:7", None),
        ("chat", "shell:3", None),
        ("chat", "schedule:9", None),
        ("chat", "system", None),
        ("chat", "system:harness", None),
        ("heartbeat", "system", None),
        ("system_note", "agent:12", {"note_tag": NoteTag.TASK.value}),
        ("resurrect", "system", None),
        ("fork", "agent:5", None),
        ("reminder", "system", None),
    ],
)
def test_non_human_wakes_from_idle_resolve_non_human_polarity(
    kind: str, source: str, payload: dict[str, object] | None
) -> None:
    idle_state = AgentState(halted=True, turn_active=False, non_human_wake=False)
    item = _inbound(1, kind=kind, source=source, payload=payload)
    assert resolve_non_human_wake(idle_state, [item], _routing(), BatchState()) is True


@pytest.mark.parametrize("initial_polarity", [False, True])
def test_mid_run_continuations_and_secondary_arrivals_preserve_polarity(
    initial_polarity: bool,
) -> None:
    active_state = AgentState(halted=False, turn_active=True, non_human_wake=initial_polarity)
    assert (
        resolve_non_human_wake(
            active_state, [_inbound(1, source="agent:99")], _routing(), BatchState()
        )
        is initial_polarity
    )

    halted_mid_run = AgentState(halted=True, turn_active=True, non_human_wake=initial_polarity)
    for cont_item in (
        _inbound(2, kind="compact_summary", source="system"),
        _inbound(3, kind="cancel", source="user"),
        _inbound(
            4,
            kind="system_note",
            source="system:restart",
            payload={"note_tag": NoteTag.LIFECYCLE_RESTART.value},
        ),
    ):
        assert (
            resolve_non_human_wake(halted_mid_run, [cont_item], _routing(), BatchState())
            is initial_polarity
        )


# meiva:note
# When a compaction summary/request wins the batch, co-batched CHAT rows are deferred back to
# `pending` by `_compact_outcome` rather than dispatched in this pass.
# meiva:link agent/docs/state.meiva.okf.md
def test_deferred_human_chat_during_compaction_does_not_flip_polarity_early() -> None:
    bg_state = AgentState(halted=True, turn_active=True, non_human_wake=True)
    st = BatchState(compact_payload=("summary", "compact_summary", None))
    batch = [
        _inbound(1, kind="compact_summary", source="system"),
        _inbound(2, kind="chat", source="user"),
    ]
    assert resolve_non_human_wake(bg_state, batch, _routing(), st) is True


def test_restart_requested_preserves_unfinished_polarity_and_resets_idle() -> None:
    unfinished_bg = AgentState(halted=False, turn_active=True, non_human_wake=True)
    st_resume = BatchState(restart_requested=True, restart_preserves_idle=False)
    assert (
        resolve_non_human_wake(
            unfinished_bg, [_inbound(1, kind="restart", source="system")], _routing(), st_resume
        )
        is True
    )

    idle_bg = AgentState(halted=True, turn_active=False, non_human_wake=True)
    st_idle = BatchState(restart_requested=True, restart_preserves_idle=True)
    assert (
        resolve_non_human_wake(
            idle_bg, [_inbound(2, kind="restart", source="user")], _routing(), st_idle
        )
        is False
    )


async def test_claim_node_impl_idle_reset_and_handoff_resume_polarity(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    del redis_url
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    runtime = make_runtime(ops_pool=aops_pool, database_gate=database_gate)
    cfg = thread_config(tid)

    cmd_end = await claim_node_impl(
        AgentState(
            messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
            halted=True,
            turn_active=True,
            non_human_wake=True,
        ),
        runtime,
        cfg,
    )
    assert cmd_end.update is not None
    assert cmd_end.update.get("non_human_wake") is False

    cmd_restart = await claim_node_impl(
        AgentState(
            messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
            halted=True,
            turn_active=True,
            interrupted_by_restart=True,
            non_human_wake=True,
        ),
        runtime,
        cfg,
    )
    assert cmd_restart.update is not None
    assert "non_human_wake" not in cmd_restart.update

    receipt = f"{tid}:1"
    handoff_note = system_note_message(
        content="External takeover session ended; resume user conversation.",
        tag=NoteTag.IMPERSONATION,
        picked_up_at=START,
    )
    handoff_note.id = f"impersonation-handoff:{receipt}"
    cmd_handoff = await claim_node_impl(
        AgentState(
            messages=[SystemMessage(content="sys"), handoff_note],
            impersonation_handoff_id=receipt,
            halted=False,
            turn_active=False,
            non_human_wake=True,
        ),
        runtime,
        cfg,
    )
    assert cmd_handoff.goto != "END"
    assert cmd_handoff.update is not None
    assert cmd_handoff.update.get("non_human_wake") is False

    peer_cmd = await settle_claimed_batch(
        runtime.context,
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        tid,
        [ClaimedInbound(id=501, agent_id=tid, content="peer ping", kind="chat", source="agent:8")],
    )
    assert peer_cmd.update is not None
    assert peer_cmd.update.get("non_human_wake") is True


def _run_background_harness_turn(live_events: LiveEvents) -> list[object]:
    moments = iter(START + timedelta(seconds=n) for n in range(6))
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=moments.__next__)
    projector = HarnessTurnProjector(
        event_publisher=live_events.publisher,
        agent_id=7,
        base_idx=2,
        turn_progress=TurnProgress(),
        clock=clock,
        non_human_wake=True,
    )
    projector.on_event(TextDelta(turn_id="t1", text="Checking worker..."))
    projector.on_event(AssistantText(turn_id="t1", text="Checking worker..."))
    projector.on_event(
        ToolUse(turn_id="t1", tool_use_id="c1", name="Bash", tool_input={"command": "ls"})
    )
    projector.on_event(AssistantText(turn_id="t1", text="Meiva agent to user: All green."))
    return list(
        projector.finish(
            (
                HarnessTurn(
                    turn_id="t1",
                    text="Meiva agent to user: All green.",
                    usage=Usage(),
                    cost_usd=None,
                    subtype="success",
                    is_error=False,
                    interrupted=False,
                ),
            )
        )
    )


async def test_harness_projector_propagates_non_human_wake_on_sse_and_messages(
    live_events: LiveEvents,
) -> None:
    from langchain_core.messages import AIMessage

    messages = _run_background_harness_turn(live_events)
    events = await live_events.events()

    chat_events = [e for e in events if e["role"] in ("chat_start", "chat_delta")]
    assert len(chat_events) >= 2
    assert {e["non_human_wake"] for e in chat_events} == {True}

    ai_msgs = [m for m in messages if isinstance(m, AIMessage)]
    assert len(ai_msgs) == 2
    assert [m.additional_kwargs.get("meiva_non_human_wake") for m in ai_msgs] == [True, True]


async def test_harness_projector_resolves_mid_turn_snapshot_polarity(
    live_events: LiveEvents,
) -> None:
    _run_background_harness_turn(live_events)
    events = await live_events.events()

    snapshots = [e for e in events if e["role"] == "timeline_snapshot"]
    assert len(snapshots) == 1
    chat_snap_items = [it for it in snapshots[0]["items"] if it["kind"] == "agent_chat"]
    assert len(chat_snap_items) == 1
    assert chat_snap_items[0]["payload"] == "Checking worker..."
    assert chat_snap_items[0]["user_facing"] is False


async def test_persist_last_active_strips_prefix_and_skips_empty_header(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    del redis_url
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    runtime = make_runtime(ops_pool=aops_pool, database_gate=database_gate)

    await persist_last_active(runtime.context, tid, "Meiva agent to user: Deployment succeeded.")
    row = db_conn.execute(
        "SELECT last_message_text FROM agents_meta WHERE id = %s", (tid,)
    ).fetchone()
    assert row == ("Deployment succeeded.",)

    await persist_last_active(runtime.context, tid, "Meiva agent to user:   ")
    row_after = db_conn.execute(
        "SELECT last_message_text FROM agents_meta WHERE id = %s", (tid,)
    ).fetchone()
    assert row_after == ("Deployment succeeded.",)
