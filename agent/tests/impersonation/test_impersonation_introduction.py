from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from agent.impersonation_handoff import (
    deliver_handoff,
    ensure_start_marker,
    note_stamp,
    note_stamp_of,
    resume_note_pending,
    start_update,
)
from agent.tests.impersonation.lease_support import Cluster, fixed_clock, idle_graph
from agent.tests.impersonation.lease_support import cluster as cluster
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig

MOMENT = datetime(2026, 10, 6, 13, 39, 25, tzinfo=UTC)


def _notes(*, timestamps: bool = False) -> TimestampPolicy:
    clock = fixed_clock()
    return TimestampPolicy(clock=clock, timestamps_enabled=timestamps)


def _session(number: int = 0) -> dict[str, Any]:
    return {
        "agent_id": 42,
        "session_id": number,
        "name": "Continue the task",
        "executor_name": "Codex",
        "relay_provider": "codex",
    }


class _FlushFailsOnce:
    def __init__(self) -> None:
        self.failed = False

    async def __call__(self, _thread_id: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("Interrupted flush")


async def test_first_takeover_explains_borrowed_identity_and_later_leases_do_not_repeat() -> None:
    graph, config = await idle_graph(42)
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


async def test_crash_after_checkpoint_before_flush_does_not_repeat_explanation() -> None:
    graph, config = await idle_graph(42)
    vars(graph.checkpointer)["_meiva_nstep_flush"] = _FlushFailsOnce()
    with pytest.raises(RuntimeError, match="Interrupted flush"):
        await ensure_start_marker(graph, _session(), notes=_notes())
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
    graph, config = await idle_graph(42)
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
    notes = TimestampPolicy(clock=clock, timestamps_enabled=timestamps)
    owner = cluster.native_agent()
    cluster.lease(owner, provider="claude", phase="released")
    lease = cluster.status(owner)
    assert lease is not None
    graph, config = await idle_graph(owner.agent_id)
    await ensure_start_marker(graph, lease, notes=notes)
    await deliver_handoff(
        graph, cluster.database, cluster.event_bus, lease, owner, notes=notes, workspace=tmp_path
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


async def test_an_existing_handoff_receipt_keeps_its_original_note(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    cluster.lease(owner, provider="claude", phase="released")
    lease = cluster.status(owner)
    assert lease is not None
    graph, config = await idle_graph(owner.agent_id)
    for _ in range(2):
        await deliver_handoff(
            graph,
            cluster.database,
            cluster.event_bus,
            lease,
            owner,
            notes=_notes(),
            workspace=tmp_path,
        )
    messages = (await graph.aget_state(config)).values["messages"]
    assert [m.id for m in messages] == [
        "initial-request",
        f"impersonation-handoff:{owner.agent_id}:0",
    ]


def test_a_note_stamp_follows_the_policy_it_is_given() -> None:
    clock = Clock(ClockConfig("UTC", "UTC", False), now=lambda: MOMENT, timers=SYSTEM_TIMERS)
    stamped = TimestampPolicy(clock=clock, timestamps_enabled=True)
    bare = TimestampPolicy(clock=clock, timestamps_enabled=False)
    assert note_stamp_of(stamped).prefix == "[2026-10-06 13:39:25] "
    assert note_stamp_of(bare).prefix == ""
    assert note_stamp_of(bare).created_at == MOMENT
    assert note_stamp(clock, timestamps=True).prefix == "[2026-10-06 13:39:25] "


def test_resume_note_pending_tracks_the_trailing_end_note() -> None:
    note = HumanMessage(content="session ended", id="impersonation-handoff:7:0")
    state = SimpleNamespace(impersonation_handoff_id="7:0", messages=[note])
    assert resume_note_pending(state)
    state.messages.append(HumanMessage(content="next task", id="m-next"))
    assert not resume_note_pending(state)
    state.impersonation_handoff_id = None
    assert not resume_note_pending(state)
    assert not resume_note_pending(SimpleNamespace(impersonation_handoff_id="9:0", messages=[]))
