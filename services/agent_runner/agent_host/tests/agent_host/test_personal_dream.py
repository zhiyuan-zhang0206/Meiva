# meiva:note
# Targeted unit and hosted end-to-end tests for per-turn Personal Self-Dream checks (task #7522):
# bounded `<workspace>/memory/` scanning, empty/auto-scaffolded index handling, capacity and
# active-change trigger rules, cooldown persistence and hardening, anti-self-trigger protection,
# valid NoteTag payload, mutual exclusion with workspace hygiene, and AgentHost turn-end wiring.

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator, Sequence
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
from base.agents.messages.envelope import validate_source
from base.agents.messages.inbound import InboundKind
from base.agents.messages.kwargs import NoteTag
from base.db import Database
from base.events.live.bus import EventBus
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_path
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.lifecycle.personal_dream import (
    check_and_remind_personal_dream,
    personal_dream_state_path,
    personal_memory_path,
    read_last_reminded_at,
    scan_personal_memory,
    write_last_reminded_at,
)
from services.agent_runner.agent_host.lifecycle.personal_dream_policy import (
    MAX_SCAN_DEPTH,
    PERSONAL_DREAM_SOURCE,
    PersonalDreamDecision,
    PersonalDreamOutcome,
    PersonalDreamTriggerReason,
    PersonalMemoryScan,
    build_personal_dream_inbound_payload,
    evaluate_personal_dream,
    format_personal_dream_reminder,
    is_cooldown_elapsed,
)
from services.agent_runner.agent_host.runtime import PersonalDreamPolicy, WorkspaceHygienePolicy
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import Rig, Row

OVERRIDE = f"{__name__}:build_test_llm"
PLANS: dict[int, list[Sequence[AIMessageChunk | BaseException]]] = {}
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


def _write_file_with_mtime(path: Path, content: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))


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


def test_scan_missing_or_empty_memory_returns_empty_without_creating_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    scan = scan_personal_memory(missing)
    assert scan.is_empty is True
    assert scan.index_lines == 0
    assert scan.file_count == 0
    assert scan.earliest_mtime is None
    assert scan.latest_mtime is None
    assert not missing.exists()

    ws = tmp_path / "ws"
    mem = personal_memory_path(ws)
    mem.mkdir(parents=True)
    _write_file_with_mtime(mem / "MEMORY.md", "   \n\n", 100.0)
    _write_file_with_mtime(mem / ".hidden.md", "hidden", 200.0)

    empty_scan = scan_personal_memory(ws)
    assert empty_scan.is_empty is True
    assert empty_scan.index_lines == 0
    assert empty_scan.file_count == 0
    assert empty_scan.earliest_mtime is None
    assert empty_scan.latest_mtime is None


def test_scan_empty_index_does_not_poison_earliest_mtime_for_first_note(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    mem = personal_memory_path(ws)
    _write_file_with_mtime(mem / "MEMORY.md", "", 100.0)
    _write_file_with_mtime(mem / "first-note.md", "# First\n", 1_000_000.0)

    scan = scan_personal_memory(ws)
    assert scan.index_lines == 0
    assert scan.file_count == 1
    assert scan.earliest_mtime == 1_000_000.0
    assert scan.latest_mtime == 1_000_000.0

    decision = evaluate_personal_dream(
        scan,
        last_reminded_at=None,
        now_epoch=1_000_010.0,
        cooldown_seconds=_SEVEN_DAYS,
        index_max_lines=150,
        max_files=40,
    )
    assert decision.should_trigger is False


def test_scan_counts_index_lines_notes_subdirs_and_ignores_dotfiles_and_symlinks(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    mem = personal_memory_path(ws)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_file_with_mtime(outside / "secret.md", "# Outside\n", 50.0)

    _write_index_lines(ws, 12, mtime=500.0)
    _write_file_with_mtime(mem / "a.md", "# A\n", 300.0)
    _write_file_with_mtime(mem / "inherit" / "rules.md", "# Rules\n", 900.0)
    _write_file_with_mtime(mem / ".DS_Store", "junk", 10.0)
    _write_file_with_mtime(mem / ".git" / "config", "git", 9999.0)
    (mem / "link_file.md").symlink_to(outside / "secret.md")
    (mem / "link_dir").symlink_to(outside)
    (mem / "loop").symlink_to(mem)

    scan = scan_personal_memory(ws)
    assert scan.index_lines == 12
    assert scan.file_count == 2
    assert scan.earliest_mtime == 300.0
    assert scan.latest_mtime == 900.0
    assert scan.truncated is False

    ws_sym = tmp_path / "ws_sym"
    ws_sym.mkdir()
    (ws_sym / "memory").symlink_to(outside)
    sym_scan = scan_personal_memory(ws_sym)
    assert sym_scan.is_empty is True


def test_scan_respects_depth_and_dir_bounds(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    mem = personal_memory_path(ws)
    d4 = mem / "d1" / "d2" / "d3" / "d4"
    d5 = d4 / "d5"
    _write_file_with_mtime(d4 / "ok.md", "# Ok\n", 100.0)
    _write_file_with_mtime(d5 / "too_deep.md", "# Deep\n", 200.0)

    depth_scan = scan_personal_memory(ws, max_depth=MAX_SCAN_DEPTH)
    assert depth_scan.file_count == 1
    assert depth_scan.latest_mtime == 100.0

    ws_cap = tmp_path / "ws_cap"
    mem_cap = personal_memory_path(ws_cap)
    for idx in range(8):
        _write_file_with_mtime(mem_cap / f"sub_{idx:02d}" / "n.md", "# N\n", 100.0)
    cap_scan = scan_personal_memory(ws_cap, max_dirs=3)
    assert cap_scan.dirs_scanned == 3
    assert cap_scan.truncated is True


def _make_scan(
    *,
    index_lines: int,
    file_count: int,
    earliest_mtime: float | None,
    latest_mtime: float | None,
) -> PersonalMemoryScan:
    return PersonalMemoryScan(
        index_lines=index_lines,
        file_count=file_count,
        earliest_mtime=earliest_mtime,
        latest_mtime=latest_mtime,
        dirs_scanned=1,
        truncated=False,
    )


def _eval(
    scan: PersonalMemoryScan,
    *,
    last_reminded_at: float | None,
    now_epoch: float,
) -> PersonalDreamDecision:
    return evaluate_personal_dream(
        scan,
        last_reminded_at=last_reminded_at,
        now_epoch=now_epoch,
        cooldown_seconds=_SEVEN_DAYS,
        index_max_lines=150,
        max_files=40,
    )


def test_evaluate_personal_dream_first_time_capacity_and_activity() -> None:
    now = 2_000_000.0

    at_cap_fresh = _make_scan(
        index_lines=150, file_count=40, earliest_mtime=now - 3600.0, latest_mtime=now - 60.0
    )
    assert _eval(at_cap_fresh, last_reminded_at=None, now_epoch=now).should_trigger is False

    over_index = _make_scan(
        index_lines=151, file_count=3, earliest_mtime=now - 10.0, latest_mtime=now - 10.0
    )
    dec_index = _eval(over_index, last_reminded_at=None, now_epoch=now)
    assert dec_index.should_trigger is True
    assert dec_index.reason is PersonalDreamTriggerReason.CAPACITY
    assert dec_index.index_over_limit is True
    assert dec_index.files_over_limit is False

    over_files = _make_scan(
        index_lines=20, file_count=41, earliest_mtime=now - 10.0, latest_mtime=now - 10.0
    )
    dec_files = _eval(over_files, last_reminded_at=None, now_epoch=now)
    assert dec_files.should_trigger is True
    assert dec_files.reason is PersonalDreamTriggerReason.CAPACITY
    assert dec_files.files_over_limit is True

    stale_idle = _make_scan(
        index_lines=10,
        file_count=5,
        earliest_mtime=now - _SEVEN_DAYS - 86400.0,
        latest_mtime=now - _SEVEN_DAYS - 1.0,
    )
    assert _eval(stale_idle, last_reminded_at=None, now_epoch=now).should_trigger is False

    first_active = _make_scan(
        index_lines=10,
        file_count=5,
        earliest_mtime=now - _SEVEN_DAYS,
        latest_mtime=now - 3600.0,
    )
    dec_first_active = _eval(first_active, last_reminded_at=None, now_epoch=now)
    assert dec_first_active.should_trigger is True
    assert dec_first_active.reason is PersonalDreamTriggerReason.ACTIVITY


def test_evaluate_personal_dream_after_previous_reminder() -> None:
    now = 2_000_000.0
    last_reminded = now - _SEVEN_DAYS

    both_over = _make_scan(
        index_lines=200,
        file_count=50,
        earliest_mtime=now - 2 * _SEVEN_DAYS,
        latest_mtime=now - 60.0,
    )
    assert (
        _eval(both_over, last_reminded_at=last_reminded + 1.0, now_epoch=now).should_trigger
        is False
    )

    dec_both = _eval(both_over, last_reminded_at=last_reminded, now_epoch=now)
    assert dec_both.should_trigger is True
    assert dec_both.reason is PersonalDreamTriggerReason.CAPACITY_AND_ACTIVITY

    unchanged = _make_scan(
        index_lines=20,
        file_count=5,
        earliest_mtime=last_reminded - 1000.0,
        latest_mtime=last_reminded,
    )
    assert _eval(unchanged, last_reminded_at=last_reminded, now_epoch=now).should_trigger is False

    modified_later = _make_scan(
        index_lines=20,
        file_count=5,
        earliest_mtime=last_reminded - 1000.0,
        latest_mtime=last_reminded + 1.0,
    )
    dec_later = _eval(modified_later, last_reminded_at=last_reminded, now_epoch=now + _SEVEN_DAYS)
    assert dec_later.should_trigger is True
    assert dec_later.reason is PersonalDreamTriggerReason.ACTIVITY


def test_is_cooldown_elapsed_boundaries() -> None:
    assert is_cooldown_elapsed(last_reminded_at=None, now_epoch=1000.0, cooldown_seconds=604800)
    assert not is_cooldown_elapsed(
        last_reminded_at=1000.0, now_epoch=605799.0, cooldown_seconds=604800
    )
    assert is_cooldown_elapsed(last_reminded_at=1000.0, now_epoch=605800.0, cooldown_seconds=604800)


def test_reminder_payload_and_message_contract() -> None:
    validate_source(PERSONAL_DREAM_SOURCE)
    payload = build_personal_dream_inbound_payload()
    assert payload == {"note_tag": NoteTag.PERSONAL_DREAM.value}
    assert NoteTag(str(payload["note_tag"])) is NoteTag.PERSONAL_DREAM

    scan = PersonalMemoryScan(
        index_lines=172,
        file_count=45,
        earliest_mtime=1000.0,
        latest_mtime=2000.0,
        dirs_scanned=2,
        truncated=False,
    )
    decision = evaluate_personal_dream(
        scan,
        last_reminded_at=1500.0,
        now_epoch=1500.0 + _SEVEN_DAYS,
        cooldown_seconds=_SEVEN_DAYS,
        index_max_lines=150,
        max_files=40,
    )
    text = format_personal_dream_reminder(
        scan,
        decision,
        index_max_lines=150,
        max_files=40,
        shared_memory_path="/home/zzy/.meiva/vault/memory",
    )
    assert "172 lines (soft cap: 150)" in text
    assert "45 files (soft cap: 40)" in text
    assert "MEMORY.md exceeds 150 lines" in text
    assert "note files exceed 40" in text
    assert "/home/zzy/.meiva/vault/memory/MEMORY.md" in text
    assert "land/<machine>" in text
    assert "#1609" in text
    assert "<= 150 lines" in text
    for step in ("1.", "2.", "3.", "4."):
        assert f"\n{step} " in text


def test_read_last_reminded_at_rejects_corrupt_utf8_json_and_non_finite_values(
    tmp_path: Path,
) -> None:
    state = personal_dream_state_path(tmp_path)

    for raw_text in (
        "{not valid json",
        "[]",
        '{"last_reminded_at": true}',
        '{"last_reminded_at": "1000"}',
        '{"last_reminded_at": NaN}',
        '{"last_reminded_at": Infinity}',
        '{"last_reminded_at": -Infinity}',
    ):
        state.write_text(raw_text, encoding="utf-8")
        assert read_last_reminded_at(tmp_path) is None

    state.write_bytes(b"\xff\xfe\xfd")
    assert read_last_reminded_at(tmp_path) is None


def test_personal_dream_handles_permission_error_on_state_file_and_subdir(tmp_path: Path) -> None:
    state = personal_dream_state_path(tmp_path)
    state.write_text('{"last_reminded_at": 1000.0}', encoding="utf-8")
    locked_dir = personal_memory_path(tmp_path) / "locked"
    _write_file_with_mtime(locked_dir / "note.md", "# Locked\n", 1000.0)

    state.chmod(0o000)
    locked_dir.chmod(0o000)
    try:
        assert read_last_reminded_at(tmp_path) is None
        scan = scan_personal_memory(tmp_path)
        assert scan.file_count == 0
    finally:
        state.chmod(0o600)
        locked_dir.chmod(0o700)


async def test_personal_dream_check_reminds_enforces_cooldown_and_blocks_self_trigger(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = 752201
    _seed_agent(db_conn, agent_id)
    ws = tmp_path / "ws"
    now = 2_000_000.0
    _write_index_lines(ws, 160, mtime=now - 10.0)
    policy = PersonalDreamPolicy(
        enabled=True,
        cooldown_seconds=_SEVEN_DAYS,
        index_max_lines=150,
        max_files=40,
    )

    disabled = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=PersonalDreamPolicy(enabled=False),
        now_epoch=now,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert disabled is PersonalDreamOutcome.DISABLED

    outcome1 = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=now,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert outcome1 is PersonalDreamOutcome.REMINDED
    assert read_last_reminded_at(ws) == now

    rows = db_conn.execute(
        "SELECT kind, source, status, payload, content FROM inbound_messages "
        "WHERE agent_id = %s ORDER BY id ASC",
        (agent_id,),
    ).fetchall()
    assert len(rows) == 1
    kind, source, status, payload, content = rows[0]
    assert kind == InboundKind.SYSTEM_NOTE.value
    assert source == PERSONAL_DREAM_SOURCE
    assert status == "pending"
    assert payload == {"note_tag": NoteTag.PERSONAL_DREAM.value}
    assert "160 lines (soft cap: 150)" in content

    cooldown_hit = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=now + 3600.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert cooldown_hit is PersonalDreamOutcome.COOLDOWN_ACTIVE

    pending_hit = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=now + _SEVEN_DAYS + 10.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert pending_hit is PersonalDreamOutcome.SKIPPED_SELF_TRIGGER

    db_conn.execute(
        "UPDATE inbound_messages SET status = 'done', claimed_at = now() "
        "WHERE agent_id = %s AND kind = 'system_note'",
        (agent_id,),
    )
    db_conn.commit()

    self_trigger = await check_and_remind_personal_dream(
        agent_id=agent_id,
        workspace_root=ws,
        policy=PersonalDreamPolicy(
            enabled=True,
            cooldown_seconds=0,
            index_max_lines=150,
            max_files=40,
        ),
        now_epoch=now + _SEVEN_DAYS + 20.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert self_trigger is PersonalDreamOutcome.SKIPPED_SELF_TRIGGER


async def test_agent_host_reminds_personal_dream_and_does_not_self_trigger(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 752202
    PLANS[agent_id] = [_ok_reply(), _ok_reply()]
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
    _populate_notes(ws, 42)

    try:
        insert_inbound_message(
            db_conn, agent_id, "Do normal work", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        dream_rows = db_conn.execute(
            "SELECT id, status, payload FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.PERSONAL_DREAM.value),
        ).fetchall()
        assert len(dream_rows) == 1
        assert dream_rows[0][1] == "pending"
        assert dream_rows[0][2] == {"note_tag": NoteTag.PERSONAL_DREAM.value}
        assert personal_dream_state_path(ws).exists()

        write_last_reminded_at(ws, 1.0)
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        dream_rows_after = db_conn.execute(
            "SELECT id, status FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.PERSONAL_DREAM.value),
        ).fetchall()
        assert len(dream_rows_after) == 1
        assert dream_rows_after[0][1] == "done"
    finally:
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
    _write_index_lines(ws, 165)

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
