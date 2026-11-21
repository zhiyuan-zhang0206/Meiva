# meiva:note
# PostgreSQL anti-self-trigger guard (including external harness `clock_timestamp()` batch claims),
# watermark state-machine, and hosted AgentHost end-to-end tests for Personal Self-Dream
# (task #7522).

from __future__ import annotations

import asyncio
import json
import os
import random
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import psycopg
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from psycopg_pool import AsyncConnectionPool

from agent.graph import build_graph
from agent.graph.exec.tests.process_inputs import live_parent_facts
from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind
from base.agents.messages.kwargs import NoteTag
from base.db import Database
from base.events.live.bus import EventBus
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_path
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.lifecycle.personal_dream import (
    check_and_remind_personal_dream,
    inspect_personal_dream_guard,
    personal_dream_state_path,
    personal_memory_path,
    read_personal_dream_state,
    write_personal_dream_state,
)
from services.agent_runner.agent_host.lifecycle.personal_dream_policy import (
    PERSONAL_DREAM_SOURCE,
    PersonalDreamOutcome,
    PersonalDreamState,
)
from services.agent_runner.agent_host.runtime import PersonalDreamPolicy, WorkspaceHygienePolicy
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import Rig, Row

OVERRIDE = f"{__name__}:build_test_llm"
PLANS: dict[int, list[Sequence[AIMessageChunk | BaseException]]] = {}
TURN_HOOKS: dict[int, list[Callable[[], None] | None]] = {}
_SEVEN_DAYS = 604800


# meiva:note
# Deterministic PRNG seed for non-cryptographic retry jitter in tests (`S311`).
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


class _ScriptedModel(BaseChatModel):
    agent_id: int

    @property
    def _llm_type(self) -> str:
        return "personal-dream-test"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("hosted graph streams asynchronously")

    async def _astream(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, args, kwargs
        hooks = TURN_HOOKS.get(self.agent_id)
        if hooks:
            hook = hooks.pop(0)
            if hook is not None:
                hook()
        steps = PLANS[self.agent_id].pop(0)
        for step in steps:
            if isinstance(step, BaseException):
                raise step
            yield ChatGenerationChunk(message=step)


def build_test_llm(model: str, *, agent_id: int | None) -> _ScriptedModel:
    del model
    if agent_id is None:
        raise ValueError("test model requires agent_id")
    return _ScriptedModel(agent_id=agent_id)


def _ok_reply(text: str = "MEIVA_HIDE_FROM_USER\nDone.") -> list[AIMessageChunk]:
    return [
        AIMessageChunk(
            content=text,
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    ]


def _seed_agent(db_conn: psycopg.Connection[Any], agent_id: int) -> None:
    db_conn.execute("INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (agent_id,))
    db_conn.execute(
        "INSERT INTO agents_meta (id, machine, status, harness) "
        "VALUES (%s, 'this-box', 'idling', 'meiva') ON CONFLICT (id) DO NOTHING",
        (agent_id,),
    )
    db_conn.commit()


def _write_index_lines(ws: Path, line_count: int, *, mtime: float | None = None) -> Path:
    index_path = personal_memory_path(ws) / "MEMORY.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"- [Note {idx}](note_{idx}.md) - desc" for idx in range(line_count))
    index_path.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(index_path, (mtime, mtime))
    return index_path


def _populate_notes(ws: Path, count: int, *, mtime: float | None = None) -> None:
    mem_dir = personal_memory_path(ws)
    mem_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(count):
        note = mem_dir / f"note_{idx:03d}.md"
        note.write_text(f"# Note {idx}\n", encoding="utf-8")
        if mtime is not None:
            os.utime(note, (mtime, mtime))


async def test_guard_matches_external_harness_clock_timestamp_batch_without_false_positive(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent_id = 752205
    _seed_agent(db_conn, agent_id)
    payload_json = json.dumps({"note_tag": NoteTag.PERSONAL_DREAM.value})

    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, payload, created_at, claimed_at) "
        "VALUES "
        "(%s, 'dream', 'system_note', %s, 'done', %s::jsonb, now() - interval '10 seconds', now() - interval '2 seconds'), "
        "(%s, 'batched chat', 'chat', 'user', 'done', NULL, now() - interval '9 seconds', now() - interval '1.995 seconds')",
        (agent_id, PERSONAL_DREAM_SOURCE, payload_json, agent_id),
    )
    db_conn.commit()

    batched_guard = await inspect_personal_dream_guard(aops_pool, agent_id)
    assert batched_guard.latest_was_personal_dream is True
    assert batched_guard.has_pending is False

    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, created_at, claimed_at) "
        "VALUES (%s, 'later turn', 'chat', 'user', 'done', now() - interval '1.9 seconds', now() - interval '1.85 seconds')",
        (agent_id,),
    )
    db_conn.commit()

    later_turn_guard = await inspect_personal_dream_guard(aops_pool, agent_id)
    assert later_turn_guard.latest_was_personal_dream is False
    assert later_turn_guard.has_pending is False


async def test_personal_dream_check_returns_disabled_when_policy_off(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = 752207
    _seed_agent(db_conn, agent_id)
    ws = tmp_path / "ws"
    _write_index_lines(ws, 160, mtime=1_999_990.0)
    disabled = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=PersonalDreamPolicy(enabled=False),
        now_epoch=2_000_000.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert disabled is PersonalDreamOutcome.DISABLED


async def test_personal_dream_watermark_advances_after_dream_turn_and_stops_self_loop(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = 752201
    _seed_agent(db_conn, agent_id)
    ws = tmp_path / "ws"
    t1 = 2_000_000.0
    _write_index_lines(ws, 160, mtime=t1 - 10.0)
    policy = PersonalDreamPolicy(
        enabled=True,
        cooldown_seconds=_SEVEN_DAYS,
        index_max_lines=150,
        max_files=40,
    )

    turn1 = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=t1,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert turn1 is PersonalDreamOutcome.REMINDED
    assert read_personal_dream_state(ws) == PersonalDreamState(
        last_reminded_at=t1,
        awaiting_dream_turn=True,
        baseline_mtime=t1 - 10.0,
    )

    rows = db_conn.execute(
        "SELECT kind, source, status, payload, content FROM inbound_messages "
        "WHERE agent_id = %s ORDER BY id ASC",
        (agent_id,),
    ).fetchall()
    assert len(rows) == 1
    kind, source, status, payload, content = rows[0]
    assert (kind, source, status, payload) == (
        InboundKind.SYSTEM_NOTE.value,
        PERSONAL_DREAM_SOURCE,
        "pending",
        {"note_tag": NoteTag.PERSONAL_DREAM.value},
    )
    assert "160 lines (soft cap: 150)" in content

    pending_turn = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=t1 + _SEVEN_DAYS + 10.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert pending_turn is PersonalDreamOutcome.SKIPPED_SELF_TRIGGER
    assert read_personal_dream_state(ws) == PersonalDreamState(
        last_reminded_at=t1,
        awaiting_dream_turn=True,
        baseline_mtime=t1 - 10.0,
    )

    t2 = t1 + _SEVEN_DAYS + 60.0
    dream_edit_mtime = t2 - 5.0
    _write_index_lines(ws, 40, mtime=dream_edit_mtime)
    db_conn.execute(
        "UPDATE inbound_messages SET status = 'done', claimed_at = now() "
        "WHERE agent_id = %s AND kind = 'system_note'",
        (agent_id,),
    )
    db_conn.commit()

    turn2 = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=t2,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert turn2 is PersonalDreamOutcome.SKIPPED_SELF_TRIGGER
    assert read_personal_dream_state(ws) == PersonalDreamState(
        last_reminded_at=t2,
        awaiting_dream_turn=False,
        baseline_mtime=dream_edit_mtime,
    )

    cooldown_turn = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=t2 + 3600.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert cooldown_turn is PersonalDreamOutcome.COOLDOWN_ACTIVE

    t3 = t2 + _SEVEN_DAYS + 10.0
    turn3 = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=t3,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert turn3 is PersonalDreamOutcome.NOT_DUE

    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, created_at, claimed_at) "
        "VALUES (%s, 'normal work', 'chat', 'user', 'done', now() + interval '1 second', now() + interval '2 seconds')",
        (agent_id,),
    )
    db_conn.commit()

    t4 = t3 + 60.0
    new_note = personal_memory_path(ws) / "new-lesson.md"
    new_note.write_text("# New lesson\n", encoding="utf-8")
    os.utime(new_note, (t4 - 2.0, t4 - 2.0))

    turn4 = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=t4,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert turn4 is PersonalDreamOutcome.REMINDED
    assert read_personal_dream_state(ws) == PersonalDreamState(
        last_reminded_at=t4,
        awaiting_dream_turn=True,
        baseline_mtime=t4 - 2.0,
    )


async def test_awaiting_dream_turn_clears_when_no_pending_and_latest_is_normal_turn(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = 752206
    _seed_agent(db_conn, agent_id)
    ws = tmp_path / "ws"
    t0 = 2_000_000.0
    _write_index_lines(ws, 40, mtime=t0 - 10.0)
    write_personal_dream_state(
        ws,
        PersonalDreamState(
            last_reminded_at=t0,
            awaiting_dream_turn=True,
            baseline_mtime=t0 - 10.0,
        ),
    )
    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, created_at, claimed_at) "
        "VALUES (%s, 'normal turn', 'chat', 'user', 'done', now() - interval '2 seconds', now() - interval '1 second')",
        (agent_id,),
    )
    db_conn.commit()

    outcome = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=PersonalDreamPolicy(enabled=True, cooldown_seconds=_SEVEN_DAYS),
        now_epoch=t0 + 60.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert outcome is PersonalDreamOutcome.COOLDOWN_ACTIVE
    assert read_personal_dream_state(ws) == PersonalDreamState(
        last_reminded_at=t0,
        awaiting_dream_turn=False,
        baseline_mtime=t0 - 10.0,
    )


async def test_agent_host_four_turn_dream_edit_watermark_lifecycle(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 752202
    PLANS[agent_id] = [_ok_reply(), _ok_reply(), _ok_reply(), _ok_reply()]
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=_rng())
    host = rig.host(
        {agent_id: Row(status="idling")},
        graph,
        catalog=None,
        policy=configured_policy(
            llm_override=OVERRIDE,
            workspace_hygiene=WorkspaceHygienePolicy(enabled=False),
            personal_dream=PersonalDreamPolicy(
                enabled=True,
                cooldown_seconds=_SEVEN_DAYS,
                index_max_lines=150,
                max_files=40,
            ),
        ),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    ws = workspace_path(host._home, agent_id)
    _write_index_lines(ws, 165)

    def _dream_turn_consolidates_memory() -> None:
        _write_index_lines(ws, 40)

    TURN_HOOKS[agent_id] = [None, _dream_turn_consolidates_memory, None, None]

    try:
        insert_inbound_message(
            db_conn, agent_id, "Turn 1 work", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        rows_t1 = db_conn.execute(
            "SELECT id, status FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.PERSONAL_DREAM.value),
        ).fetchall()
        assert len(rows_t1) == 1
        assert rows_t1[0][1] == "pending"
        state_t1 = read_personal_dream_state(ws)
        assert state_t1 is not None and state_t1.awaiting_dream_turn is True

        await asyncio.wait_for(host.run_turn(agent_id), 15)

        rows_t2 = db_conn.execute(
            "SELECT id, status FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.PERSONAL_DREAM.value),
        ).fetchall()
        assert len(rows_t2) == 1
        assert rows_t2[0][1] == "done"
        state_t2 = read_personal_dream_state(ws)
        assert state_t2 is not None
        assert state_t2.awaiting_dream_turn is False
        assert state_t2.baseline_mtime is not None

        write_personal_dream_state(
            ws,
            PersonalDreamState(
                last_reminded_at=state_t2.last_reminded_at - (_SEVEN_DAYS + 60.0),
                awaiting_dream_turn=False,
                baseline_mtime=state_t2.baseline_mtime,
            ),
        )
        insert_inbound_message(
            db_conn,
            agent_id,
            "Turn 3 after 7d with no new edits",
            "user",
            bus=event_bus,
            database=database,
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        rows_t3 = db_conn.execute(
            "SELECT id, status FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.PERSONAL_DREAM.value),
        ).fetchall()
        assert len(rows_t3) == 1

        new_note = personal_memory_path(ws) / "after-dream.md"
        new_note.write_text("# New personal note\n", encoding="utf-8")
        new_mtime = state_t2.baseline_mtime + 10.0
        os.utime(new_note, (new_mtime, new_mtime))

        insert_inbound_message(
            db_conn,
            agent_id,
            "Turn 4 after new personal note",
            "user",
            bus=event_bus,
            database=database,
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        rows_t4 = db_conn.execute(
            "SELECT id, status FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.PERSONAL_DREAM.value),
        ).fetchall()
        assert len(rows_t4) == 2
        assert rows_t4[1][1] == "pending"
        state_t4 = read_personal_dream_state(ws)
        assert state_t4 is not None and state_t4.awaiting_dream_turn is True
    finally:
        TURN_HOOKS.pop(agent_id, None)
        await host.aclose()


async def test_agent_host_skips_personal_dream_after_aborted_turn(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 752203
    PLANS[agent_id] = [
        [
            AIMessageChunk(
                content="partial",
                response_metadata={"model_provider": "anthropic"},
            )
        ]
    ]
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=_rng())
    host = rig.host(
        {agent_id: Row(status="idling")},
        graph,
        catalog=None,
        policy=configured_policy(
            llm_override=OVERRIDE,
            workspace_hygiene=WorkspaceHygienePolicy(enabled=False),
            personal_dream=PersonalDreamPolicy(enabled=True),
        ),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    ws = workspace_path(host._home, agent_id)
    _write_index_lines(ws, 170)

    try:
        insert_inbound_message(
            db_conn, agent_id, "Aborting turn", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        dream_rows = db_conn.execute(
            "SELECT id FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s",
            (agent_id, NoteTag.PERSONAL_DREAM.value),
        ).fetchall()
        assert dream_rows == []
        assert not personal_dream_state_path(ws).exists()
    finally:
        await host.aclose()


async def test_agent_host_defers_personal_dream_when_workspace_hygiene_reminds_same_turn(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 752204
    PLANS[agent_id] = [_ok_reply(), _ok_reply()]
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=_rng())
    host = rig.host(
        {agent_id: Row(status="idling")},
        graph,
        catalog=None,
        policy=configured_policy(
            llm_override=OVERRIDE,
            workspace_hygiene=WorkspaceHygienePolicy(
                enabled=True, max_items_per_dir=20, remind_cooldown_seconds=86400
            ),
            personal_dream=PersonalDreamPolicy(
                enabled=True, cooldown_seconds=_SEVEN_DAYS, index_max_lines=150, max_files=40
            ),
        ),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    ws = workspace_path(host._home, agent_id)
    ws.mkdir(parents=True, exist_ok=True)
    for idx in range(22):
        (ws / f"clutter_{idx:03d}.txt").write_text("x", encoding="utf-8")
    _populate_notes(ws, 42)

    try:
        insert_inbound_message(
            db_conn, agent_id, "Normal turn", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        tags_after_turn1 = [
            row[0]
            for row in db_conn.execute(
                "SELECT payload ->> 'note_tag' FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'system_note' ORDER BY id ASC",
                (agent_id,),
            ).fetchall()
        ]
        assert tags_after_turn1 == [NoteTag.WORKSPACE_HYGIENE.value]
        assert not personal_dream_state_path(ws).exists()

        await asyncio.wait_for(host.run_turn(agent_id), 15)

        tags_after_turn2 = [
            row[0]
            for row in db_conn.execute(
                "SELECT payload ->> 'note_tag' FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'system_note' ORDER BY id ASC",
                (agent_id,),
            ).fetchall()
        ]
        assert tags_after_turn2 == [
            NoteTag.WORKSPACE_HYGIENE.value,
            NoteTag.PERSONAL_DREAM.value,
        ]
        assert personal_dream_state_path(ws).exists()
    finally:
        await host.aclose()
