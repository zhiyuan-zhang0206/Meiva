# meiva:note
# A refusal at the old turn's terminal boundary ends that delivery run. The inbox owns the retry,
# so the next run consumes the same row once without losing or duplicating its timeline entry.
# meiva:link agent/graph/docs/harness-foreground.md

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    completed,
    in_a_turn,
    input_key,
    insert_row,
    row_status,
    runtime,
    timeline_inbound_ids,
    wait_until,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.test_harness_turn import config
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.record.read import read_window
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import TimelineSnapshot
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    BridgeEvent,
    FileSessionRegistry,
    HarnessInput,
    InputInContext,
    InputRejected,
    SessionRecord,
    TurnOrigin,
    TurnStarted,
)
from base.lm.harness.antigravity.timing_store import store_timings, timing_path
from base.lm.harness.antigravity.trajectory import parse_trajectory
from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.session.transcript.record_file import RecordFile, UnreadRows
from base.paths import harness_sessions_dir, workspace_dir


async def test_a_turn_boundary_rejection_hands_the_input_to_the_next_run_once(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    first = insert_row(db_conn, agent_id, "chat", "user", "finish this turn")

    def at_the_boundary(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        [entry] = inputs
        if entry.key == input_key(first):
            return []
        if engine.subscribe_count == 1:
            return [
                InputRejected(
                    entry.key,
                    "the running turn has finished; hand the input over again",
                    1,
                ),
                completed("t1", "MEIVA_DISPLAY_TO_USER\nfirst done"),
            ]
        return in_a_turn([entry.key], "MEIVA_DISPLAY_TO_USER\nsecond done", turn_id="t2")

    engine = FakeEngine(script=at_the_boundary)
    rt = harness_runtime(engine, agent_id=agent_id)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        first_run = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: len(engine.delivered) == 1, "first delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (input_key(first),), 1),
            InputInContext(input_key(first), "t1", 1),
        )
        second = insert_row(db_conn, agent_id, "chat", "user", "start the next turn")
        engine.notify_inbound()
        await first_run

    assert [row_status(db_conn, i) for i in (first, second)] == ["done", "pending"]
    second_deliveries = [
        item.key for batch in engine.delivered for item in batch if item.key == input_key(second)
    ]
    assert second_deliveries == [input_key(second)], "the finishing run makes one attempt"

    async with asyncio.timeout(5):
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, second) == "done"
    second_deliveries = [
        item.key for batch in engine.delivered for item in batch if item.key == input_key(second)
    ]
    assert second_deliveries == [input_key(second), input_key(second)]
    assert (await timeline_inbound_ids(aops_pool, agent_id)).count(second) == 1


@pytest.mark.parametrize("preexisting_cache", [False, True])
async def test_timing_revision_emits_only_same_id_reasoning_updates(
    tmp_path: Path,
    live_events: LiveEvents,
    preexisting_cache: bool,
) -> None:
    session = "native-timing"
    path = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / session
        / ".system_generated/logs/transcript_full.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "step_index": 0,
                "created_at": "2026-11-17T05:30:00Z",
                "thinking": "synthetic thought",
                "content": "MEIVA_DISPLAY_TO_USER\nanswer",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo fixture"}}],
            }
        )
        + "\n"
    )
    native = parse_trajectory(
        {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": 1,
            "trajectory": {
                "steps": [
                    {
                        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                        "status": "CORTEX_STEP_STATUS_DONE",
                        "metadata": {"createdAt": "2026-11-17T05:30:00.123Z"},
                        "plannerResponse": {
                            "thinking": "synthetic thought",
                            "thinkingDuration": "30.265829s",
                        },
                    }
                ]
            },
        }
    )
    if preexisting_cache:
        store_timings(path, native)
    ctx = MeivaContext(
        agent=AgentSlices.resolve(default_reader=defaults()),
        harness=Harness.ANTIGRAVITY,
        meiva_home=tmp_path,
        environ={"HOME": str(tmp_path)},
    )
    sink = ReadTimelineSink(
        ctx, 100, live_events.publisher, non_human_wake=False, session_id=lambda: session
    )
    initial, end = sink._project_new()
    assert initial is not None
    old = next(item.item for item in initial.items if item.item.kind == "agent_reasoning")
    assert old.reasoning_ms == (30265 if preexisting_cache else None)
    assert await live_events.events() == []
    if not preexisting_cache:
        store_timings(path, native)
        await sink._refresh_timing(end)
        assert_reasoning_update(await live_events.typed(), old.item_id, 30265)
    assert sink.has_activity is False
    await sink._refresh_timing(end)
    assert await live_events.events() == []
    timing_path(path).unlink()
    await sink._refresh_timing(end)
    assert_reasoning_update(await live_events.typed(), old.item_id, None)
    await sink._refresh_timing(end)
    assert await live_events.events() == []


def assert_reasoning_update(events: Sequence[object], item_id: str, ms: int | None) -> None:
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TimelineSnapshot)
    assert len(event.items) == 1
    assert event.items[0]["item_id"] == item_id
    assert event.items[0]["kind"] == "agent_reasoning"
    assert event.items[0]["reasoning_ms"] == ms


def _claude_row(
    *,
    row_type: str,
    uuid_value: str,
    prompt_id: str | None = None,
    text: str,
    session_id: str,
    cwd: str,
) -> str:
    stamp = datetime.now(UTC).isoformat()
    if row_type == "user":
        payload: dict[str, object] = {
            "type": "user",
            "uuid": uuid_value,
            "promptId": prompt_id,
            "timestamp": stamp,
            "isSidechain": False,
            "sessionId": session_id,
            "cwd": cwd,
            "message": {"role": "user", "content": text},
        }
    else:
        payload = {
            "type": "assistant",
            "uuid": uuid_value,
            "timestamp": stamp,
            "isSidechain": False,
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "id": f"msg_{uuid_value[:8]}",
                "model": "claude-sonnet-5-5",
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}],
            },
        }
    return json.dumps(payload) + "\n"


def _publications(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    db_conn.commit()
    rows = db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [(str(key), str(content)) for key, content in rows]


def _fork_pair(
    tmp_path: Path, db_conn: psycopg.Connection, *, parent_session: str
) -> tuple[int, Path]:
    parent_id = agent_row(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='claude_code' WHERE id=%s", (parent_id,))
    db_conn.commit()
    parent_ws = workspace_dir(parent_id, home=tmp_path / "home")
    parent_folder = tmp_path / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(parent_ws))
    parent_folder.mkdir(parents=True)
    registry = FileSessionRegistry(harness_sessions_dir(home=tmp_path / "home"))
    now = datetime.now(UTC).isoformat(timespec="seconds")
    registry.save(
        SessionRecord(
            agent_id=parent_id,
            session_id=parent_session,
            cwd=str(parent_ws),
            model="claude-sonnet-5-5",
            created_at=now,
            updated_at=now,
            harness="claude_code",
            generation=1,
        )
    )
    (parent_folder / f"{parent_session}.jsonl").write_text(
        _claude_row(
            row_type="user",
            uuid_value="11111111-1111-4111-8111-111111111111",
            prompt_id="parent-prompt-1",
            text="parent question",
            session_id=parent_session,
            cwd=str(parent_ws),
        )
        + _claude_row(
            row_type="assistant",
            uuid_value="22222222-2222-4222-8222-222222222222",
            text="MEIVA_DISPLAY_TO_USER\nparent reply one",
            session_id=parent_session,
            cwd=str(parent_ws),
        )
        + _claude_row(
            row_type="assistant",
            uuid_value="33333333-3333-4333-8333-333333333333",
            text="MEIVA_DISPLAY_TO_USER\nparent reply two",
            session_id=parent_session,
            cwd=str(parent_ws),
        ),
        encoding="utf-8",
    )
    child_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, spawner, born_spawner, fork_source_agent_id, "
        "fork_source_checkpoint_id, status, harness) "
        "VALUES (%s, %s, %s, %s, 'ckpt', 'running', 'claude_code')",
        (child_id, f"agent:{parent_id}", f"agent:{parent_id}", parent_id),
    )
    db_conn.commit()
    child_ws = workspace_dir(child_id, home=tmp_path / "home")
    child_folder = tmp_path / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(child_ws))
    child_folder.mkdir(parents=True)
    return child_id, child_folder


def _fork_sink_ctx(tmp_path: Path, database_gate: ProcessDbGate) -> MeivaContext:
    base = runtime(
        None,
        database_gate=database_gate,
        event_bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
    ).context
    return replace(
        base,
        agent=AgentSlices.resolve({"user_delivery_mode": "prefix"}, default_reader=defaults()),
        harness=Harness.CLAUDE_CODE,
        meiva_home=tmp_path / "home",
        environ={"HOME": str(tmp_path)},
    )


async def test_fork_sink_init_before_jsonl_materialization_never_publishes_parent_display_rows(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    live_events: LiveEvents,
) -> None:
    parent_session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    child_session = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    child_id, child_folder = _fork_pair(tmp_path, db_conn, parent_session=parent_session)
    child_ws = workspace_dir(child_id, home=tmp_path / "home")
    child_file = child_folder / f"{child_session}.jsonl"
    assert not child_file.exists()

    ctx = _fork_sink_ctx(tmp_path, database_gate)
    sink = ReadTimelineSink(
        ctx,
        child_id,
        live_events.publisher,
        non_human_wake=False,
        session_id=lambda: child_session,
    )
    assert sink._history_end == 0

    child_file.write_text(
        json.dumps({"type": "mode", "mode": "normal", "sessionId": child_session})
        + "\n"
        + _claude_row(
            row_type="user",
            uuid_value="11111111-1111-4111-8111-111111111111",
            prompt_id="child-prompt-1",
            text="parent question",
            session_id=child_session,
            cwd=str(child_ws),
        )
        + _claude_row(
            row_type="assistant",
            uuid_value="22222222-2222-4222-8222-222222222222",
            text="MEIVA_DISPLAY_TO_USER\nparent reply one",
            session_id=child_session,
            cwd=str(child_ws),
        )
        + _claude_row(
            row_type="assistant",
            uuid_value="33333333-3333-4333-8333-333333333333",
            text="MEIVA_DISPLAY_TO_USER\nparent reply two",
            session_id=child_session,
            cwd=str(child_ws),
        ),
        encoding="utf-8",
    )
    await sink._read()
    assert _publications(db_conn, child_id) == [], "parent prefix alone must not publish"

    with child_file.open("a", encoding="utf-8") as handle:
        handle.write(
            _claude_row(
                row_type="user",
                uuid_value=key_uuid("inbound-1"),
                prompt_id="child-prompt-1",
                text="child prompt 1",
                session_id=child_session,
                cwd=str(child_ws),
            )
            + _claude_row(
                row_type="assistant",
                uuid_value="44444444-4444-4444-8444-444444444444",
                text="MEIVA_DISPLAY_TO_USER\nchild reply one",
                session_id=child_session,
                cwd=str(child_ws),
            )
        )
    await sink.finish(input_keys=["inbound-1"])

    assert _publications(db_conn, child_id) == [
        ("harness:claude:44444444-4444-4444-8444-444444444444", "child reply one")
    ]

    cold = read_window(
        RecordFile(child_file),
        harness=Harness.CLAUDE_CODE.value,
        limit=50,
        before=None,
        policy=ReplyPolicy(explicit_visibility=True, delivery_mode="prefix"),
        render_tool=format_tool_use,
        load_sent=lambda _until, _wanted: {},
        unread=UnreadRows(),
    )
    assert [i.item.payload for i in cold.items if i.item.kind == "agent_chat"] == [
        "parent reply one",
        "parent reply two",
        "child reply one",
    ]

    later_sink = ReadTimelineSink(
        ctx,
        child_id,
        live_events.publisher,
        non_human_wake=False,
        session_id=lambda: child_session,
    )
    later_sink._from = 0
    with child_file.open("a", encoding="utf-8") as handle:
        handle.write(
            _claude_row(
                row_type="user",
                uuid_value=key_uuid("inbound-2"),
                prompt_id="child-prompt-2",
                text="child prompt 2",
                session_id=child_session,
                cwd=str(child_ws),
            )
            + _claude_row(
                row_type="assistant",
                uuid_value="55555555-5555-4555-8555-555555555555",
                text="MEIVA_DISPLAY_TO_USER\nchild reply two",
                session_id=child_session,
                cwd=str(child_ws),
            )
        )
    await later_sink.finish(input_keys=["inbound-2"])

    assert _publications(db_conn, child_id) == [
        ("harness:claude:44444444-4444-4444-8444-444444444444", "child reply one"),
        ("harness:claude:55555555-5555-4555-8555-555555555555", "child reply two"),
    ]


async def test_fork_sink_without_input_keys_falls_back_to_parent_uuids_and_hide_turn_stays_quiet(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    live_events: LiveEvents,
) -> None:
    parent_session = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    child_session = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    child_id, child_folder = _fork_pair(tmp_path, db_conn, parent_session=parent_session)
    child_ws = workspace_dir(child_id, home=tmp_path / "home")
    child_file = child_folder / f"{child_session}.jsonl"

    ctx = _fork_sink_ctx(tmp_path, database_gate)
    sink = ReadTimelineSink(
        ctx,
        child_id,
        live_events.publisher,
        non_human_wake=False,
        session_id=lambda: child_session,
    )
    assert sink._history_end == 0

    child_file.write_text(
        _claude_row(
            row_type="user",
            uuid_value="11111111-1111-4111-8111-111111111111",
            prompt_id="child-prompt-1",
            text="parent question",
            session_id=child_session,
            cwd=str(child_ws),
        )
        + _claude_row(
            row_type="assistant",
            uuid_value="22222222-2222-4222-8222-222222222222",
            text="MEIVA_DISPLAY_TO_USER\nparent reply one",
            session_id=child_session,
            cwd=str(child_ws),
        )
        + _claude_row(
            row_type="user",
            uuid_value="77777777-7777-4777-8777-777777777777",
            prompt_id="child-prompt-1",
            text="child hidden prompt",
            session_id=child_session,
            cwd=str(child_ws),
        )
        + _claude_row(
            row_type="assistant",
            uuid_value="88888888-8888-4888-8888-888888888888",
            text="MEIVA_HIDE_FROM_USER\ninternal note only",
            session_id=child_session,
            cwd=str(child_ws),
        ),
        encoding="utf-8",
    )
    await sink.finish()
    assert _publications(db_conn, child_id) == []

    turn_2_sink = ReadTimelineSink(
        ctx,
        child_id,
        live_events.publisher,
        non_human_wake=False,
        session_id=lambda: child_session,
    )
    turn_2_sink._from = 0
    with child_file.open("a", encoding="utf-8") as handle:
        handle.write(
            _claude_row(
                row_type="user",
                uuid_value="99999999-9999-4999-8999-999999999999",
                prompt_id="child-prompt-2",
                text="child visible prompt",
                session_id=child_session,
                cwd=str(child_ws),
            )
            + _claude_row(
                row_type="assistant",
                uuid_value="12121212-1212-4121-8121-121212121212",
                text="MEIVA_DISPLAY_TO_USER\nchild turn two answer",
                session_id=child_session,
                cwd=str(child_ws),
            )
        )
    await turn_2_sink.finish()

    assert _publications(db_conn, child_id) == [
        ("harness:claude:12121212-1212-4121-8121-121212121212", "child turn two answer")
    ]
