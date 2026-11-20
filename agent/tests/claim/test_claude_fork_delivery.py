from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    Script,
    config,
    in_a_turn,
    insert_row,
    row_status,
    runtime,
)
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import HostedTurnResources, MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    BridgeEvent,
    FileSessionRegistry,
    HarnessInput,
    SessionRecord,
)
from base.lm.harness.claude.delivery import key_uuid
from base.paths import harness_sessions_dir, workspace_dir
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


def _engine(session_id: str, script: Script | None = None) -> FakeEngine:
    engine = FakeEngine(script=script or (lambda _inputs: []))
    engine.session_id = session_id
    return engine


def _claude_context(
    tmp_path: Path,
    engine: FakeEngine,
    pool: AsyncConnectionPool,
    resources: HostedTurnResources,
    gate: ProcessDbGate,
    live: LiveEvents,
) -> Runtime[MeivaContext]:
    base = runtime(engine, ops_pool=pool, hosted_resources=resources, publisher=live.publisher)
    return Runtime(
        context=replace(
            base.context,
            harness=Harness.CLAUDE_CODE,
            meiva_home=tmp_path / "home",
            environ={"HOME": str(tmp_path)},
            db=Database.from_settings(gate=gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            agent=AgentSlices.resolve({"user_delivery_mode": "prefix"}, default_reader=defaults()),
        )
    )


def _publications(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    db_conn.commit()
    rows: list[tuple[object, ...]] = db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [(str(key), str(content)) for key, content, *_ in rows]


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


def _create_claude_fork_pair(
    tmp_path: Path, db_conn: psycopg.Connection, *, parent_session: str
) -> tuple[int, int, Path]:
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
    parent_file = parent_folder / f"{parent_session}.jsonl"
    parent_file.write_text(
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
    return parent_id, child_id, child_folder


async def test_claude_fork_turn_publishes_only_child_replies_through_harness_turn_node(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    parent_session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    child_session = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    _parent_id, child_id, child_folder = _create_claude_fork_pair(
        tmp_path, db_conn, parent_session=parent_session
    )
    child_ws = workspace_dir(child_id, home=tmp_path / "home")
    row = insert_row(db_conn, child_id, "chat", "user", "child hello")

    def fork_cli(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        key = inputs[0].key
        child_file = child_folder / f"{child_session}.jsonl"
        child_file.write_text(
            _claude_row(
                row_type="user",
                uuid_value="11111111-1111-4111-8111-111111111111",
                prompt_id="child-turn-1",
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
                uuid_value=key_uuid(key),
                prompt_id="child-turn-1",
                text="child hello",
                session_id=child_session,
                cwd=str(child_ws),
            )
            + _claude_row(
                row_type="assistant",
                uuid_value="66666666-6666-4666-8666-666666666666",
                text="MEIVA_DISPLAY_TO_USER\nchild own answer",
                session_id=child_session,
                cwd=str(child_ws),
            ),
            encoding="utf-8",
        )
        return in_a_turn([item.key for item in inputs], "MEIVA_DISPLAY_TO_USER\nchild own answer")

    engine = _engine(child_session, fork_cli)
    rt = _claude_context(tmp_path, engine, aops_pool, hosted_resources, database_gate, live_events)
    command = await harness_turn_node(AgentState(), rt, config(child_id))
    assert command.goto == "claim"
    assert row_status(db_conn, row) == "done"
    assert _publications(db_conn, child_id) == [
        ("harness:claude:66666666-6666-4666-8666-666666666666", "child own answer")
    ]
