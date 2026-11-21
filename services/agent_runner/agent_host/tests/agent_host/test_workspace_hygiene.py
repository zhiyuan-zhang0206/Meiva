# meiva:note
# Targeted unit and hosted end-to-end tests for per-turn workspace directory hygiene checks
# (task #7521): bounded local os.scandir scanning, exclusion rules, cooldown persistence and
# reset, anti-self-trigger protection, valid NoteTag payload, and AgentHost turn-end wiring.

from __future__ import annotations

import asyncio
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
from services.agent_runner.agent_host.lifecycle.workspace_hygiene import (
    check_and_remind_workspace_hygiene,
    hygiene_state_path,
    read_last_reminded_at,
    scan_workspace_overflows,
    write_last_reminded_at,
)
from services.agent_runner.agent_host.lifecycle.workspace_hygiene_policy import (
    MAX_REPORTED_DIRS,
    MAX_SCAN_DEPTH,
    WORKSPACE_HYGIENE_SOURCE,
    DirectoryOverflow,
    HygieneCheckOutcome,
    build_hygiene_inbound_payload,
    format_workspace_hygiene_reminder,
    should_remind,
)
from services.agent_runner.agent_host.runtime import WorkspaceHygienePolicy
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import Rig, Row

OVERRIDE = f"{__name__}:build_test_llm"
PLANS: dict[int, list[Sequence[AIMessageChunk | BaseException]]] = {}


# meiva:note
# Deterministic PRNG seed for non-cryptographic retry jitter in tests (`S311`).
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


class _ScriptedModel(BaseChatModel):
    agent_id: int

    @property
    def _llm_type(self) -> str:
        return "workspace-hygiene-test"

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


def _populate_files(directory: Path, count: int, *, prefix: str = "file") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for idx in range(count):
        (directory / f"{prefix}_{idx:03d}.txt").write_text("x", encoding="utf-8")


def _seed_agent(db_conn: psycopg.Connection[Any], agent_id: int) -> None:
    db_conn.execute("INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (agent_id,))
    db_conn.execute(
        "INSERT INTO agents_meta (id, machine, status, harness) "
        "VALUES (%s, 'this-box', 'idling', 'meiva') ON CONFLICT (id) DO NOTHING",
        (agent_id,),
    )
    db_conn.commit()


def test_scan_detects_root_and_subdir_overflows_sorted_descending(tmp_path: Path) -> None:
    _populate_files(tmp_path, 20)
    assert scan_workspace_overflows(tmp_path, max_items_per_dir=20).overflows == ()

    _populate_files(tmp_path / "sub_a", 25)
    _populate_files(tmp_path / "sub_b", 21)

    result = scan_workspace_overflows(tmp_path, max_items_per_dir=20)
    assert result.overflows == (
        DirectoryOverflow(relative_path="sub_a", item_count=25),
        DirectoryOverflow(relative_path=".", item_count=22),
        DirectoryOverflow(relative_path="sub_b", item_count=21),
    )
    assert result.truncated is False


def test_scan_excludes_dotfiles_system_root_paths_nested_git_repos_and_build_dirs(
    tmp_path: Path,
) -> None:
    _populate_files(tmp_path, 18)

    (tmp_path / ".hidden_file").write_text("x", encoding="utf-8")
    (tmp_path / ".meiva_workspace_hygiene.json").write_text("{}", encoding="utf-8")
    for dot_dir in (".git", ".venv", ".pytest_cache", ".ruff_cache", ".shell_logs", ".worktrees"):
        _populate_files(tmp_path / dot_dir, 25)

    (tmp_path / "meiva_instructions.md").write_text("instructions", encoding="utf-8")
    _populate_files(tmp_path / "memory", 30)
    _populate_files(tmp_path / "uploads", 30)

    for build_dir in ("node_modules", "__pycache__", "dist", "build", "target", "venv"):
        _populate_files(tmp_path / build_dir, 30)

    repo_dir = tmp_path / "nested_repo"
    _populate_files(repo_dir, 40)
    (repo_dir / ".git").mkdir()

    worktree_dir = tmp_path / "nested_worktree"
    _populate_files(worktree_dir, 40)
    (worktree_dir / ".git").write_text("gitdir: /tmp/fake", encoding="utf-8")

    result = scan_workspace_overflows(tmp_path, max_items_per_dir=20)
    assert result.overflows == ()

    (tmp_path / "extra.txt").write_text("x", encoding="utf-8")
    result_after = scan_workspace_overflows(tmp_path, max_items_per_dir=20)
    assert result_after.overflows == (DirectoryOverflow(relative_path=".", item_count=21),)


def test_scan_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _populate_files(outside, 30)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "link_to_outside").symlink_to(outside)
    (ws / "loop").symlink_to(ws)

    result = scan_workspace_overflows(ws, max_items_per_dir=20)
    assert result.overflows == ()


def test_scan_respects_max_depth_and_max_dirs_bounds(tmp_path: Path) -> None:
    d4 = tmp_path / "d1" / "d2" / "d3" / "d4"
    d5 = d4 / "d5"
    _populate_files(d4, 21)
    _populate_files(d5, 25)

    depth_result = scan_workspace_overflows(
        tmp_path, max_items_per_dir=20, max_depth=MAX_SCAN_DEPTH
    )
    assert depth_result.overflows == (
        DirectoryOverflow(relative_path="d1/d2/d3/d4", item_count=22),
    )

    bounded_root = tmp_path / "bounded"
    _populate_files(bounded_root, 21)
    for idx in range(12):
        _populate_files(bounded_root / f"dir_{idx:02d}", 25)

    cap_result = scan_workspace_overflows(bounded_root, max_items_per_dir=20, max_dirs=5)
    assert cap_result.dirs_scanned == 5
    assert cap_result.truncated is True
    assert any(item.relative_path == "." for item in cap_result.overflows)


def test_scan_missing_workspace_returns_empty_without_creating_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    result = scan_workspace_overflows(missing, max_items_per_dir=20)
    assert result.overflows == ()
    assert result.dirs_scanned == 0
    assert not missing.exists()


def test_reminder_payload_and_message_contract() -> None:
    validate_source(WORKSPACE_HYGIENE_SOURCE)
    payload = build_hygiene_inbound_payload()
    assert payload == {"note_tag": NoteTag.WORKSPACE_HYGIENE.value}
    assert NoteTag(str(payload["note_tag"])) is NoteTag.WORKSPACE_HYGIENE

    overflows = [
        DirectoryOverflow(relative_path="." if idx == 0 else f"sub_{idx}", item_count=30 - idx)
        for idx in range(MAX_REPORTED_DIRS + 3)
    ]
    text = format_workspace_hygiene_reminder(overflows, max_items_per_dir=20)
    assert ". (workspace root): 30 items" in text
    assert "... and 3 more directories over the limit." in text
    assert "to_user.md" in text
    assert "<= 20 items" in text


def test_should_remind_cooldown_boundaries() -> None:
    assert should_remind(last_reminded_at=None, now_epoch=1000.0, cooldown_seconds=86400) is True
    assert (
        should_remind(last_reminded_at=1000.0, now_epoch=87399.0, cooldown_seconds=86400) is False
    )
    assert should_remind(last_reminded_at=1000.0, now_epoch=87400.0, cooldown_seconds=86400) is True


def test_read_last_reminded_at_rejects_corrupt_utf8_json_and_non_finite_values(
    tmp_path: Path,
) -> None:
    state = hygiene_state_path(tmp_path)

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


def test_hygiene_handles_permission_error_on_state_file_and_subdir(tmp_path: Path) -> None:
    state = hygiene_state_path(tmp_path)
    state.write_text('{"last_reminded_at": 1000.0}', encoding="utf-8")
    locked_dir = tmp_path / "locked"
    _populate_files(locked_dir, 25)

    state.chmod(0o000)
    locked_dir.chmod(0o000)
    try:
        assert read_last_reminded_at(tmp_path) is None
        result = scan_workspace_overflows(tmp_path, max_items_per_dir=20)
        assert result.overflows == ()
    finally:
        state.chmod(0o600)
        locked_dir.chmod(0o700)


async def test_hygiene_check_reminds_and_enforces_24h_cooldown(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = 752101
    _seed_agent(db_conn, agent_id)
    ws = tmp_path / "ws"
    _populate_files(ws, 22)
    policy = WorkspaceHygienePolicy(
        enabled=True, max_items_per_dir=20, remind_cooldown_seconds=86400
    )

    disabled = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=WorkspaceHygienePolicy(enabled=False),
        now_epoch=1000.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert disabled is HygieneCheckOutcome.DISABLED

    outcome1 = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=1000.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert outcome1 is HygieneCheckOutcome.REMINDED
    assert read_last_reminded_at(ws) == 1000.0

    rows = db_conn.execute(
        "SELECT kind, source, status, payload, content FROM inbound_messages "
        "WHERE agent_id = %s ORDER BY id ASC",
        (agent_id,),
    ).fetchall()
    assert len(rows) == 1
    kind, source, status, payload, content = rows[0]
    assert kind == InboundKind.SYSTEM_NOTE.value
    assert source == WORKSPACE_HYGIENE_SOURCE
    assert status == "pending"
    assert payload == {"note_tag": NoteTag.WORKSPACE_HYGIENE.value}
    assert ". (workspace root): 22 items" in content

    outcome2 = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=1000.0 + 3600.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert outcome2 is HygieneCheckOutcome.COOLDOWN_ACTIVE


async def test_hygiene_check_resets_cooldown_after_cleanup_to_within_limit(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = 752105
    _seed_agent(db_conn, agent_id)
    ws = tmp_path / "ws"
    _populate_files(ws, 15)
    write_last_reminded_at(ws, 1000.0)
    policy = WorkspaceHygienePolicy(
        enabled=True, max_items_per_dir=20, remind_cooldown_seconds=86400
    )

    outcome_clean = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=1060.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert outcome_clean is HygieneCheckOutcome.CLEAN_RESET
    assert not hygiene_state_path(ws).exists()

    _populate_files(ws, 21, prefix="new")
    outcome_again = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=1120.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert outcome_again is HygieneCheckOutcome.REMINDED
    assert read_last_reminded_at(ws) == 1120.0


async def test_hygiene_check_blocks_pending_and_self_trigger_turns(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = 752104
    _seed_agent(db_conn, agent_id)
    ws = tmp_path / "ws"
    _populate_files(ws, 22)
    policy = WorkspaceHygienePolicy(
        enabled=True, max_items_per_dir=20, remind_cooldown_seconds=86400
    )

    first = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=1000.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert first is HygieneCheckOutcome.REMINDED

    pending = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=policy,
        now_epoch=1000.0 + 90000.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert pending is HygieneCheckOutcome.SKIPPED_SELF_TRIGGER

    db_conn.execute(
        "UPDATE inbound_messages SET status = 'done', claimed_at = now() "
        "WHERE agent_id = %s AND kind = 'system_note'",
        (agent_id,),
    )
    db_conn.commit()

    self_triggered = await check_and_remind_workspace_hygiene(
        agent_id=agent_id,
        workspace_root=ws,
        policy=WorkspaceHygienePolicy(
            enabled=True, max_items_per_dir=20, remind_cooldown_seconds=0
        ),
        now_epoch=1000.0 + 90001.0,
        pool=aops_pool,
        db=database,
        bus=event_bus,
    )
    assert self_triggered is HygieneCheckOutcome.SKIPPED_SELF_TRIGGER


async def test_agent_host_reminds_after_normal_turn_and_does_not_self_trigger(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 752102
    PLANS[agent_id] = [_ok_reply(), _ok_reply(), _ok_reply()]
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=_rng())
    host = rig.host(
        {agent_id: Row(status="idling")},
        graph,
        catalog=None,
        policy=configured_policy(
            llm_override=OVERRIDE,
            workspace_hygiene=WorkspaceHygienePolicy(
                enabled=True,
                max_items_per_dir=20,
                remind_cooldown_seconds=86400,
            ),
        ),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    ws = workspace_path(host._home, agent_id)
    _populate_files(ws, 22)

    try:
        insert_inbound_message(
            db_conn, agent_id, "Do some work", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        hygiene_rows = db_conn.execute(
            "SELECT id, status, payload FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.WORKSPACE_HYGIENE.value),
        ).fetchall()
        assert len(hygiene_rows) == 1
        assert hygiene_rows[0][1] == "pending"
        assert hygiene_rows[0][2] == {"note_tag": NoteTag.WORKSPACE_HYGIENE.value}
        assert hygiene_state_path(ws).exists()

        write_last_reminded_at(ws, 1.0)
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        hygiene_rows_after = db_conn.execute(
            "SELECT id, status FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s ORDER BY id ASC",
            (agent_id, NoteTag.WORKSPACE_HYGIENE.value),
        ).fetchall()
        assert len(hygiene_rows_after) == 1
        assert hygiene_rows_after[0][1] == "done"

        for idx in range(12):
            (ws / f"file_{idx:03d}.txt").unlink()
        insert_inbound_message(
            db_conn, agent_id, "Follow-up task", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)
        assert not hygiene_state_path(ws).exists()
    finally:
        await host.aclose()


async def test_agent_host_does_not_check_workspace_hygiene_after_aborted_turn(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 752103
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
            workspace_hygiene=WorkspaceHygienePolicy(
                enabled=True,
                max_items_per_dir=20,
                remind_cooldown_seconds=86400,
            ),
        ),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    ws = workspace_path(host._home, agent_id)
    _populate_files(ws, 25)

    try:
        insert_inbound_message(
            db_conn, agent_id, "Aborting turn", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)

        hygiene_rows = db_conn.execute(
            "SELECT id FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' "
            "AND payload ->> 'note_tag' = %s",
            (agent_id, NoteTag.WORKSPACE_HYGIENE.value),
        ).fetchall()
        assert hygiene_rows == []
        assert not hygiene_state_path(ws).exists()
    finally:
        await host.aclose()
