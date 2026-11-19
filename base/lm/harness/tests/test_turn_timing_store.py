from __future__ import annotations

from pathlib import Path

from base.lm.harness.registry import (
    ActiveTurnRecord,
    FileSessionRegistry,
    SessionRecord,
    TurnTimingStore,
)


def test_open_close_load_and_session_isolation(tmp_path: Path) -> None:
    store = TurnTimingStore(tmp_path)

    store.open_turn(
        42,
        "claude_code",
        turn_id="t1",
        started_at="2026-11-19T07:00:00+00:00",
        start_offset=0,
        session_id="s1",
    )
    spans, active = store.load(42, "claude_code", session_id="s1")
    assert spans == ()
    assert active == ActiveTurnRecord(
        turn_id="t1",
        started_at="2026-11-19T07:00:00+00:00",
        start_offset=0,
        session_id="s1",
    )

    store.close_turn(
        42,
        "claude_code",
        session_id="s1",
        turn_id="t1",
        start_offset=0,
        end_offset=450,
        started_at="2026-11-19T07:00:00+00:00",
        ended_at="2026-11-19T07:00:05+00:00",
        duration_ms=5000,
    )
    store.close_turn(
        42,
        "claude_code",
        session_id="s2",
        turn_id="other-session-turn",
        start_offset=0,
        end_offset=200,
        started_at="2026-11-19T08:00:00+00:00",
        ended_at="2026-11-19T08:00:09+00:00",
        duration_ms=9000,
    )

    spans_s1, active_after = store.load(42, "claude_code", session_id="s1")
    assert active_after is None
    assert len(spans_s1) == 1
    assert spans_s1[0].turn_id == "t1"
    assert spans_s1[0].start_offset == 0
    assert spans_s1[0].end_offset == 450
    assert spans_s1[0].duration_ms == 5000
    path_s1 = store.turns_path(42, "claude_code", session_id="s1")
    path_s2 = store.turns_path(42, "claude_code", session_id="s2")
    assert path_s1 != path_s2
    assert path_s1.is_file() and path_s2.is_file()
    path_s2.unlink()
    spans_s1_only, _ = store.load(42, "claude_code", session_id="s1")
    assert len(spans_s1_only) == 1


def test_registry_delete_removes_turn_timing_sidecars(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path)
    registry.save(
        SessionRecord(
            agent_id=7,
            harness="codex",
            session_id="sess-7",
            cwd=str(tmp_path),
            model="m",
            created_at="2026-11-19T07:00:00+00:00",
            updated_at="2026-11-19T07:00:00+00:00",
            generation=1,
        )
    )
    store = TurnTimingStore(tmp_path)
    store.open_turn(
        7,
        "codex",
        turn_id="t1",
        started_at="2026-11-19T07:00:00+00:00",
        start_offset=0,
        session_id="sess-7",
    )
    store.close_turn(
        7,
        "codex",
        session_id="sess-7",
        turn_id="t1",
        start_offset=0,
        end_offset=300,
        started_at="2026-11-19T07:00:00+00:00",
        ended_at="2026-11-19T07:00:04+00:00",
        duration_ms=4000,
    )
    store.open_turn(
        7,
        "codex",
        turn_id="t2",
        started_at="2026-11-19T07:10:00+00:00",
        start_offset=300,
        session_id="sess-7",
    )

    assert registry.delete(7) is True
    assert not store.turns_path(7, "codex", session_id="sess-7").exists()
    assert not store.active_path(7, "codex").exists()
