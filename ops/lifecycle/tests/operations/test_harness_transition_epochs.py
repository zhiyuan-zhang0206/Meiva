import json
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.epochs.extent import extent_reader
from base.agents.epochs.model import RecordExtent
from base.agents.epochs.read import read_epochs
from base.lm.catalog import ModelCatalog
from base.lm.harness.registry import FileSessionRegistry, SessionRecord
from ops.lifecycle.restart_row import update_harness_transition_meta_blocking
from ops.rpc_schemas import RestartAgentRequest


def _request(overlay: dict[str, object], *, source: str = "user") -> RestartAgentRequest:
    return RestartAgentRequest(source=source, message="hand over the build", config_overlay=overlay)


def _lengths(agent_id: int, harness: str) -> RecordExtent | None:
    return RecordExtent("claude-session", 120) if harness == "claude_code" else None


def _grown_lengths(_agent_id: int, harness: str) -> RecordExtent | None:
    return RecordExtent("claude-session", 480) if harness == "claude_code" else None


def test_an_operator_switch_closes_and_opens_epochs_with_the_harness_update(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    model_catalog: ModelCatalog,
) -> None:
    overlay: dict[str, object] = {"harness": "claude_code", "llm_model": "claude-fable-5-2"}
    update_harness_transition_meta_blocking(
        db_pool, lifecycle_agent_id, "claude_code", _request(overlay), model_catalog, _lengths
    )
    update_harness_transition_meta_blocking(
        db_pool,
        lifecycle_agent_id,
        "meiva",
        _request({"harness": "meiva", "llm_model": "deepseek-flash"}, source="self"),
        model_catalog,
        _grown_lengths,
    )

    db_conn.commit()
    first, second, third = read_epochs(db_conn, lifecycle_agent_id)
    assert [(e.seq, e.harness, e.switched_by) for e in (first, second, third)] == [
        (0, "meiva", "birth"),
        (1, "claude_code", "user"),
        (2, "meiva", "self"),
    ]
    assert (second.session_id, second.source_start, second.source_end) == (
        "claude-session",
        120,
        480,
    )
    assert (second.model, second.reason) == ("claude-fable-5-2", "hand over the build")
    assert third.ended_at is None
    harness_row = db_conn.execute(
        "SELECT harness FROM agents_meta WHERE id = %s", (lifecycle_agent_id,)
    ).fetchone()
    assert harness_row == ("meiva",)


def test_a_restart_that_keeps_the_harness_opens_no_epoch(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    model_catalog: ModelCatalog,
) -> None:
    update_harness_transition_meta_blocking(
        db_pool,
        lifecycle_agent_id,
        "meiva",
        _request({"llm_model": "deepseek-flash"}),
        model_catalog,
        _lengths,
    )

    db_conn.commit()
    [only] = read_epochs(db_conn, lifecycle_agent_id)
    assert (only.seq, only.ended_at) == (0, None)


def test_a_failure_inside_the_switch_leaves_the_harness_and_the_epochs_as_they_were(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    model_catalog: ModelCatalog,
) -> None:
    def unreadable(agent_id: int, harness: str) -> RecordExtent | None:
        raise OSError("record disk unavailable")

    with pytest.raises(OSError, match="record disk unavailable"):
        update_harness_transition_meta_blocking(
            db_pool,
            lifecycle_agent_id,
            "codex",
            _request({"harness": "codex", "llm_model": "gpt-6.2-sol"}),
            model_catalog,
            unreadable,
        )

    db_conn.commit()
    [only] = read_epochs(db_conn, lifecycle_agent_id)
    assert only.ended_at is None
    assert db_conn.execute(
        "SELECT harness FROM agents_meta WHERE id = %s", (lifecycle_agent_id,)
    ).fetchone() == ("meiva",)


def _claude_record(home: Path, agent_id: int, workspace: Path) -> SessionRecord:
    return SessionRecord(
        agent_id=agent_id,
        session_id="sess-1",
        cwd=str(workspace),
        model="claude-fable-5-2",
        created_at="2026-11-25T00:00:00+00:00",
        updated_at="2026-11-25T00:00:00+00:00",
        harness="claude_code",
        generation=1,
    )


def test_the_extent_is_the_length_of_the_record_file_the_session_record_names(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    claude_home = tmp_path / ".claude"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = FileSessionRegistry(sessions)
    registry.save(_claude_record(tmp_path, 41, workspace))
    environ = {"HOME": str(tmp_path)}
    read = extent_reader(registry, environ)

    assert read(41, "claude_code") == RecordExtent("sess-1", 0)
    assert read(41, "codex") is None
    assert read(41, "meiva") is None
    assert read(42, "claude_code") is None

    slug = "".join(c if c.isalnum() else "-" for c in str(workspace))
    record = claude_home / "projects" / slug / "sess-1.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"type": "user"}) + "\n", encoding="utf-8")

    assert read(41, "claude_code") == RecordExtent("sess-1", record.stat().st_size)
