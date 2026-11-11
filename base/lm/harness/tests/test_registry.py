from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from base.lm.harness import FileSessionRegistry, SessionRecord
from base.lm.harness.registry import (
    backfill_record_fields,
    migrate_single_slot_records,
    unreadable_records,
)

HARNESS = "claude_code"


def record(
    agent_id: int = 6984,
    *,
    session_id: str = "sess-1",
    resumes: int = 0,
    harness: str = HARNESS,
    updated_at: str = "2026-10-19T08:00:00+00:00",
) -> SessionRecord:
    return SessionRecord(
        agent_id=agent_id,
        session_id=session_id,
        cwd="/workspace/agent",
        model="claude-sonnet-5-5",
        created_at="2026-10-19T08:00:00+00:00",
        updated_at=updated_at,
        harness=harness,
        resumes=resumes,
        generation=0,
    )


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    original = record()
    registry.save(original)
    assert registry.load(6984, harness=HARNESS) == original


def test_save_replaces_the_previous_record_atomically(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record())
    registry.save(record(session_id="sess-2", resumes=1))
    assert registry.load(6984, harness=HARNESS) == record(session_id="sess-2", resumes=1)
    assert sorted(path.name for path in (tmp_path / "sessions").iterdir()) == [
        "6984.claude_code.json"
    ]


def test_missing_record_is_none(tmp_path: Path) -> None:
    assert FileSessionRegistry(tmp_path / "sessions").load(4242, harness=HARNESS) is None


def test_each_harness_keeps_its_own_record(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record(session_id="claude-session", harness="claude_code"))
    registry.save(record(session_id="codex-session", harness="codex"))
    registry.save(record(session_id="claude-session-2", harness="claude_code"))

    claude = registry.load(6984, harness="claude_code")
    codex = registry.load(6984, harness="codex")
    assert claude is not None and claude.session_id == "claude-session-2"
    assert codex is not None and codex.session_id == "codex-session"
    assert registry.load(6984, harness="antigravity") is None
    assert registry.load(6985, harness="codex") is None


def test_agent_ids_that_share_a_prefix_do_not_share_records(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record(agent_id=1))
    registry.save(record(agent_id=10))
    assert [r.agent_id for r in registry.records(1)] == [1]
    assert registry.delete(1) is True
    assert registry.load(10, harness=HARNESS) is not None


def test_latest_is_the_most_recently_updated_record(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record(harness="codex", updated_at="2026-10-20T08:00:00+00:00"))
    registry.save(record(harness="claude_code", updated_at="2026-10-21T08:00:00+00:00"))
    latest = registry.latest(6984)
    assert latest is not None and latest.harness == "claude_code"
    assert registry.latest(1) is None


def test_corrupt_record_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    path = registry.path(6984, HARNESS)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        registry.load(6984, harness=HARNESS)


def test_non_object_payload_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    path = registry.path(6984, HARNESS)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([6984, "sess-1"]), encoding="utf-8")
    with pytest.raises(TypeError):
        registry.load(6984, harness=HARNESS)


def test_a_record_without_a_harness_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    path = registry.path(6984, HARNESS)
    path.parent.mkdir(parents=True)
    document = {
        "agent_id": 6984,
        "session_id": "x",
        "cwd": "/w",
        "model": "m",
        "created_at": "t",
        "updated_at": "t",
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="misses"):
        registry.load(6984, harness=HARNESS)


def test_delete_removes_every_harness_of_the_agent(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record(harness="claude_code"))
    registry.save(record(harness="codex"))
    assert registry.delete(6984) is True
    assert registry.delete(6984) is False
    assert registry.records(6984) == []


def test_non_positive_agent_id_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    with pytest.raises(ValueError):
        registry.path(0, HARNESS)


def test_an_unusable_harness_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lowercase identifier"):
        FileSessionRegistry(tmp_path / "sessions").path(6984, "../evil")


# meiva:note
# The single-slot layout (`<agent_id>.json`) is gone from the code: one converge-time migration
# converts it, and a file left in it makes a read refuse instead of looking like no session.
# meiva:link base/lm/harness/docs/harness.md
def single_slot(root: Path, agent_id: int = 6984, **overrides: object) -> Path:
    document: dict[str, object] = {
        "agent_id": agent_id,
        "session_id": "sess-old",
        "cwd": "/workspace/agent",
        "model": "claude-sonnet-5-5",
        "created_at": "2026-10-19T08:00:00+00:00",
        "updated_at": "2026-10-19T08:00:00+00:00",
        "resumes": 2,
        "harness": "codex",
    }
    document.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{agent_id}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_migration_moves_a_single_slot_record_to_its_harness(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    old = single_slot(root)

    assert migrate_single_slot_records(root) == [old]

    assert not old.exists()
    loaded = FileSessionRegistry(root).load(6984, harness="codex")
    assert loaded is not None
    assert (loaded.session_id, loaded.resumes) == ("sess-old", 2)


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    single_slot(root)
    single_slot(root, agent_id=7001, harness="claude_code")
    assert len(migrate_single_slot_records(root)) == 2
    assert migrate_single_slot_records(root) == []
    assert sorted(p.name for p in root.iterdir()) == ["6984.codex.json", "7001.claude_code.json"]


def test_the_migration_names_a_record_without_a_harness_the_builtin_one(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = single_slot(root)
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["harness"]
    path.write_text(json.dumps(document), encoding="utf-8")

    _ = migrate_single_slot_records(root)

    assert FileSessionRegistry(root).load(6984, harness="meiva") is not None


def test_a_run_cut_short_between_write_and_removal_is_finished_by_the_next(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    old = single_slot(root)
    FileSessionRegistry(root).save(replace(record(harness="codex"), session_id="sess-old"))
    assert old.exists()

    assert migrate_single_slot_records(root) == [old]
    assert not old.exists()
    assert len(list(root.iterdir())) == 1


def test_the_migration_keeps_a_new_layout_record_over_an_old_one(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    single_slot(root)
    FileSessionRegistry(root).save(replace(record(harness="codex"), session_id="sess-newer"))

    _ = migrate_single_slot_records(root)

    loaded = FileSessionRegistry(root).load(6984, harness="codex")
    assert loaded is not None and loaded.session_id == "sess-newer"


def test_the_migration_refuses_a_file_it_cannot_read_and_names_it(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _ = single_slot(root, agent_id=1)
    bad = root / "2.json"
    bad.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match=r"2\.json"):
        migrate_single_slot_records(root)

    assert bad.exists()


def test_the_migration_refuses_a_record_naming_another_agent(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    single_slot(root, agent_id=5)
    (root / "5.json").write_text(
        (root / "5.json").read_text(encoding="utf-8").replace('"agent_id": 5', '"agent_id": 6'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="names agent 6"):
        migrate_single_slot_records(root)


def test_the_migration_of_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    assert migrate_single_slot_records(tmp_path / "absent") == []


def test_an_unmigrated_single_slot_file_makes_a_read_refuse(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    single_slot(root)
    with pytest.raises(ValueError, match="meiva converge"):
        FileSessionRegistry(root).load(6984, harness="codex")


def test_the_generation_survives_a_roundtrip_and_a_record_without_it_is_refused(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(replace(record(), generation=5))
    assert registry.load(6984, harness=HARNESS) == replace(record(), generation=5)

    path = registry.path(6984, HARNESS)
    payload = json.loads(path.read_text())
    del payload["generation"]
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="generation"):
        _ = registry.load(6984, harness=HARNESS)


def test_converge_gives_an_older_record_generation_zero_once(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(record())
    path = registry.path(6984, HARNESS)
    payload = json.loads(path.read_text())
    del payload["generation"]
    path.write_text(json.dumps(payload))

    assert backfill_record_fields(tmp_path / "sessions") == [path]
    assert backfill_record_fields(tmp_path / "sessions") == []
    loaded = registry.load(6984, harness=HARNESS)
    assert loaded is not None and loaded.generation == 0


def test_a_home_with_a_single_slot_record_or_an_unreadable_one_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    assert unreadable_records(root) == []
    old = single_slot(root)
    (root / "7.codex.json").write_text(json.dumps({"agent_id": 7}), encoding="utf-8")

    problems = unreadable_records(root)

    assert len(problems) == 2
    assert f"{old}: single-slot layout" in problems
    assert any("7.codex.json" in line and "misses" in line for line in problems)

    _ = migrate_single_slot_records(root)
    (root / "7.codex.json").unlink()
    assert unreadable_records(root) == []


# meiva:note
# A record written before the `background` field existed is refused on read, not read as empty: the
# converge step backfills it, rewriting only the files that lack it, and a second run is a no-op.
# meiva:link base/lm/harness/docs/background-work.md
def test_a_record_without_background_is_refused_until_converge_backfills_it(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    registry = FileSessionRegistry(root)
    registry.save(record(6984))
    path = registry.path(6984, HARNESS)
    document = json.loads(path.read_text())
    del document["background"]
    path.write_text(json.dumps(document))
    current = registry.path(6985, HARNESS)
    registry.save(record(6985))

    with pytest.raises(ValueError, match="misses"):
        _ = registry.load(6984, harness=HARNESS)

    assert backfill_record_fields(root) == [path]
    assert backfill_record_fields(root) == []
    loaded = registry.load(6984, harness=HARNESS)
    assert loaded is not None and loaded.background == ()
    assert registry.load(6985, harness=HARNESS) == record(6985)
    assert current.exists()


def test_a_record_missing_background_is_reported_as_unreadable_like_one_missing_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    registry = FileSessionRegistry(root)
    registry.save(record(6984))
    registry.save(record(6985, harness="codex"))
    for agent_id, harness, field in ((6984, HARNESS, "background"), (6985, "codex", "generation")):
        path = registry.path(agent_id, harness)
        document = json.loads(path.read_text())
        del document[field]
        path.write_text(json.dumps(document))

    problems = unreadable_records(root)

    assert len(problems) == 2
    assert any("6984" in line and "background" in line for line in problems)
    assert any("6985" in line and "generation" in line for line in problems)
    assert len(backfill_record_fields(root)) == 2
    assert unreadable_records(root) == []
