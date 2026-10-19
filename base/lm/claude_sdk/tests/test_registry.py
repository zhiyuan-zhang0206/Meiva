from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.lm.claude_sdk import FileSessionRegistry, SessionRecord


def record(agent_id: int = 6984, *, session_id: str = "sess-1", resumes: int = 0) -> SessionRecord:
    return SessionRecord(
        agent_id=agent_id,
        session_id=session_id,
        cwd="/workspace/agent",
        model="claude-sonnet-5-5",
        created_at="2026-10-19T08:00:00+00:00",
        updated_at="2026-10-19T08:00:00+00:00",
        resumes=resumes,
    )


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    original = record()
    registry.save(original)
    assert registry.load(6984) == original


def test_save_replaces_the_previous_record_atomically(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record())
    registry.save(record(session_id="sess-2", resumes=1))
    assert registry.load(6984) == record(session_id="sess-2", resumes=1)
    assert sorted(path.name for path in (tmp_path / "sessions").iterdir()) == ["6984.json"]


def test_missing_record_is_none(tmp_path: Path) -> None:
    assert FileSessionRegistry(tmp_path / "sessions").load(4242) is None


def test_corrupt_record_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    path = registry.path(6984)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        registry.load(6984)


def test_non_object_payload_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    path = registry.path(6984)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([6984, "sess-1"]), encoding="utf-8")
    with pytest.raises(TypeError):
        registry.load(6984)


def test_unknown_field_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    path = registry.path(6984)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"agent_id": 6984, "session_id": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="misses"):
        registry.load(6984)


def test_delete_reports_whether_a_record_existed(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record())
    assert registry.delete(6984) is True
    assert registry.delete(6984) is False


def test_non_positive_agent_id_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    with pytest.raises(ValueError):
        registry.path(0)
