from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from psycopg_pool import AsyncConnectionPool

from agent.impersonation_handoff import (
    deliver_handoff,
    ensure_start_marker,
    note_stamp,
    note_stamp_of,
    resume_note_pending,
    start_update,
)
from agent.state import BaseAgentState
from agent.tests.impersonation.lease_support import (
    Cluster,
    checkpoint_inserts_failing,
    fixed_clock,
    idle_graph,
    throttled_saver,
)
from agent.tests.impersonation.lease_support import cluster as cluster
from base.agents.messages.timestamp_policy import TimestampPolicy, TimestampSource
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.tests.fakes import clock_rejecting_now

MOMENT = datetime(2026, 10, 6, 13, 39, 25, tzinfo=UTC)


def _notes(*, timestamps: bool = False) -> TimestampPolicy:
    clock = fixed_clock()
    return TimestampPolicy.from_clock(clock, timestamps_enabled=timestamps)


def _session(number: int = 0) -> dict[str, Any]:
    return {
        "agent_id": 42,
        "session_id": number,
        "name": "Continue the task",
        "executor_name": "Codex",
        "relay_provider": "codex",
    }


async def test_first_takeover_explains_borrowed_identity_and_later_leases_do_not_repeat() -> None:
    graph, config = await idle_graph(42, MemorySaver())
    await ensure_start_marker(graph, _session(), notes=_notes())
    first = await graph.aget_state(config)
    assert first.values["impersonation_introduced"] is True
    explanation = first.values["messages"][-2]
    assert explanation.id == "impersonation-introduction"
    assert "You are paused" in explanation.content
    assert "receives incoming messages" in explanation.content
    assert "coming from you, an Meiva agent" in explanation.content
    assert "continue any unfinished requests" in explanation.content
    assert first.values["messages"][-1].id == "impersonation-start:42:0"

    await graph.aupdate_state(
        config, {"messages": [HumanMessage(id="next-request", content="More work")]}
    )
    await ensure_start_marker(graph, _session(), notes=_notes())
    await ensure_start_marker(graph, _session(1), notes=_notes())
    await ensure_start_marker(graph, _session(1), notes=_notes())
    messages = (await graph.aget_state(config)).values["messages"]
    assert sum(m.id == "impersonation-introduction" for m in messages) == 1
    assert sum(m.id == "impersonation-start:42:0" for m in messages) == 1
    assert sum(m.id == "impersonation-start:42:1" for m in messages) == 1


async def test_a_failed_flush_leaves_nothing_durable_and_the_retry_explains_once(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any]
) -> None:
    saver = await throttled_saver(aops_pool)
    graph, config = await idle_graph(42, saver)
    with (
        checkpoint_inserts_failing(db_conn),
        pytest.raises(psycopg.errors.RaiseException, match="checkpoint inserts refused"),
    ):
        await ensure_start_marker(graph, _session(), notes=_notes())
    assert "impersonation_introduced" not in (await graph.aget_state(config)).values
    await ensure_start_marker(graph, _session(), notes=_notes())
    snapshot = await graph.aget_state(config)
    assert snapshot.values["impersonation_introduced"] is True
    assert [m.id for m in snapshot.values["messages"][-2:]] == [
        "impersonation-introduction",
        "impersonation-start:42:0",
    ]
    assert len(snapshot.values["messages"]) == 3


async def test_old_checkpoint_with_start_marker_gets_explanation_without_rewriting_history() -> (
    None
):
    graph, config = await idle_graph(42, MemorySaver())
    stamp = note_stamp_of(_notes())
    [marker] = start_update(_session(), introduced=True, stamp=stamp)["messages"]
    await graph.aupdate_state(
        config, {"messages": [marker, HumanMessage(id="saved-work", content="Saved work")]}
    )
    before = (await graph.aget_state(config)).values["messages"]
    await ensure_start_marker(graph, _session(), notes=_notes())
    snapshot = await graph.aget_state(config)
    assert snapshot.values["messages"][:-1] == before
    assert snapshot.values["messages"][-1].id == "impersonation-introduction"
    assert snapshot.values["impersonation_introduced"] is True


@pytest.mark.parametrize("timestamps,weekday", [(True, False), (True, True), (False, False)])
async def test_all_takeover_notes_stamp_their_real_creation_time(
    cluster: Cluster, tmp_path: Path, timestamps: bool, weekday: bool
) -> None:
    clock = Clock(
        ClockConfig("America/New_York", "America/New_York", weekday),
        now=lambda: MOMENT,
        timers=SYSTEM_TIMERS,
    )
    notes = TimestampPolicy.from_clock(clock, timestamps_enabled=timestamps)
    owner = cluster.native_agent()
    cluster.lease(owner, provider="claude", phase="released")
    lease = cluster.status(owner)
    assert lease is not None
    graph, config = await idle_graph(owner.agent_id, MemorySaver())
    await ensure_start_marker(graph, lease, notes=notes)
    await deliver_handoff(
        graph,
        cluster.database,
        cluster.event_bus,
        lease,
        owner,
        timestamps=TimestampSource(clock=clock, timestamps_enabled=timestamps),
        workspace=tmp_path,
    )
    notes_written = (await graph.aget_state(config)).values["messages"][1:]
    assert [note.id for note in notes_written] == [
        "impersonation-introduction",
        f"impersonation-start:{owner.agent_id}:0",
        f"impersonation-handoff:{owner.agent_id}:0",
    ]
    assert "Reply to the user in ordinary assistant text" in notes_written[-1].content
    date = "2026-10-06 Tue" if weekday else "2026-10-06"
    prefix = f"<system_note>\n[{date} 09:39:25] " if timestamps else "<system_note>\nImpersonation"
    for note in notes_written:
        assert str(note.content).startswith(prefix)
        assert note.additional_kwargs["meiva_created_at"] == MOMENT.isoformat()
        assert note.additional_kwargs["meiva_note_tag"] == "impersonation"


async def test_a_handoff_receipt_replay_keeps_the_note_without_observing_time(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    cluster.lease(owner, provider="claude", phase="released")
    lease = cluster.status(owner)
    assert lease is not None
    graph, config = await idle_graph(owner.agent_id, MemorySaver())
    await deliver_handoff(
        graph,
        cluster.database,
        cluster.event_bus,
        lease,
        owner,
        timestamps=TimestampSource(clock=fixed_clock(), timestamps_enabled=True),
        workspace=tmp_path,
    )
    original = [
        message.model_dump() for message in (await graph.aget_state(config)).values["messages"]
    ]
    # meiva:note
    # The checkpoint landed but the database receipt was lost at the crash boundary.
    cluster.db_conn.execute(
        "UPDATE agent_impersonations SET handoff_applied_at=NULL WHERE id=%s", (lease["id"],)
    )
    cluster.db_conn.commit()
    await deliver_handoff(
        graph,
        cluster.database,
        cluster.event_bus,
        lease,
        owner,
        timestamps=TimestampSource(clock=clock_rejecting_now(), timestamps_enabled=True),
        workspace=tmp_path,
    )
    assert [
        message.model_dump() for message in (await graph.aget_state(config)).values["messages"]
    ] == original
    assert [message["id"] for message in original] == [
        "initial-request",
        f"impersonation-handoff:{owner.agent_id}:0",
    ]
    receipt = cluster.db_conn.execute(
        "SELECT handoff_applied_at FROM agent_impersonations WHERE id=%s", (lease["id"],)
    ).fetchone()
    assert receipt is not None and receipt[0] is not None


async def test_a_new_handoff_observes_time_before_it_writes_the_note_or_receipt(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    cluster.lease(owner, provider="claude", phase="released")
    lease = cluster.status(owner)
    assert lease is not None
    graph, config = await idle_graph(owner.agent_id, MemorySaver())
    with pytest.raises(AssertionError, match="must not observe wall time"):
        await deliver_handoff(
            graph,
            cluster.database,
            cluster.event_bus,
            lease,
            owner,
            timestamps=TimestampSource(clock=clock_rejecting_now(), timestamps_enabled=True),
            workspace=tmp_path,
        )
    assert [message.id for message in (await graph.aget_state(config)).values["messages"]] == [
        "initial-request"
    ]
    receipt = cluster.db_conn.execute(
        "SELECT handoff_applied_at FROM agent_impersonations WHERE id=%s", (lease["id"],)
    ).fetchone()
    assert receipt is not None and receipt[0] is None


def test_a_note_stamp_follows_the_policy_it_is_given() -> None:
    clock = Clock(ClockConfig("UTC", "UTC", False), now=lambda: MOMENT, timers=SYSTEM_TIMERS)
    stamped = TimestampPolicy.from_clock(clock, timestamps_enabled=True)
    bare = TimestampPolicy.from_clock(clock, timestamps_enabled=False)
    assert note_stamp_of(stamped).prefix == "[2026-10-06 13:39:25] "
    assert note_stamp_of(bare).prefix == ""
    assert note_stamp_of(bare).created_at == MOMENT
    assert note_stamp(clock, timestamps=True).prefix == "[2026-10-06 13:39:25] "


def test_resume_note_pending_tracks_the_trailing_end_note() -> None:
    note = HumanMessage(content="session ended", id="impersonation-handoff:7:0")
    state = BaseAgentState(impersonation_handoff_id="7:0", messages=[note])
    assert resume_note_pending(state)
    state.messages.append(HumanMessage(content="next task", id="m-next"))
    assert not resume_note_pending(state)
    state.impersonation_handoff_id = None
    assert not resume_note_pending(state)
    assert not resume_note_pending(BaseAgentState(impersonation_handoff_id="9:0", messages=[]))
