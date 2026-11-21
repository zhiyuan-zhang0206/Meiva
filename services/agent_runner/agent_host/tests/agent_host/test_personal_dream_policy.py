# meiva:note
# Unit tests for Personal Self-Dream pure policy, bounded `<workspace>/memory/` scanner, state
# JSON hardening (corrupt JSON/UTF-8, NaN/Infinity, OverflowError, PermissionError, legacy
# fallback), and 4-step reminder message contract (task #7522).

from __future__ import annotations

import os
from pathlib import Path

from base.agents.messages.envelope import validate_source
from base.agents.messages.kwargs import NoteTag
from services.agent_runner.agent_host.lifecycle.personal_dream import (
    personal_dream_state_path,
    personal_memory_path,
    read_personal_dream_state,
    scan_personal_memory,
    write_personal_dream_state,
)
from services.agent_runner.agent_host.lifecycle.personal_dream_policy import (
    MAX_SCAN_DEPTH,
    PERSONAL_DREAM_SOURCE,
    PersonalDreamDecision,
    PersonalDreamState,
    PersonalDreamTriggerReason,
    PersonalMemoryScan,
    build_personal_dream_inbound_payload,
    evaluate_personal_dream,
    format_personal_dream_reminder,
    is_cooldown_elapsed,
)

_SEVEN_DAYS = 604800


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
    baseline_mtime: float | None = None,
) -> PersonalDreamDecision:
    state = (
        PersonalDreamState(last_reminded_at=last_reminded_at, baseline_mtime=baseline_mtime)
        if last_reminded_at is not None
        else None
    )
    return evaluate_personal_dream(
        scan,
        state=state,
        now_epoch=now_epoch,
        cooldown_seconds=_SEVEN_DAYS,
        index_max_lines=150,
        max_files=40,
    )


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
    assert _eval(scan, last_reminded_at=None, now_epoch=1_000_010.0).should_trigger is False


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
    assert scan_personal_memory(ws_sym).is_empty is True


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


def test_evaluate_personal_dream_after_previous_reminder_and_baseline_mtime() -> None:
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

    dec_both = _eval(
        both_over,
        last_reminded_at=last_reminded,
        baseline_mtime=last_reminded + 5.0,
        now_epoch=now,
    )
    assert dec_both.should_trigger is True
    assert dec_both.reason is PersonalDreamTriggerReason.CAPACITY_AND_ACTIVITY

    dream_edited_scan = _make_scan(
        index_lines=20,
        file_count=5,
        earliest_mtime=last_reminded - 1000.0,
        latest_mtime=last_reminded + 15.0,
    )
    assert (
        _eval(
            dream_edited_scan,
            last_reminded_at=last_reminded,
            baseline_mtime=last_reminded + 15.0,
            now_epoch=now,
        ).should_trigger
        is False
    )

    new_edit_scan = _make_scan(
        index_lines=20,
        file_count=5,
        earliest_mtime=last_reminded - 1000.0,
        latest_mtime=last_reminded + 16.0,
    )
    dec_after_baseline = _eval(
        new_edit_scan,
        last_reminded_at=last_reminded,
        baseline_mtime=last_reminded + 15.0,
        now_epoch=now + _SEVEN_DAYS,
    )
    assert dec_after_baseline.should_trigger is True
    assert dec_after_baseline.reason is PersonalDreamTriggerReason.ACTIVITY

    dec_legacy = _eval(
        new_edit_scan,
        last_reminded_at=last_reminded,
        baseline_mtime=None,
        now_epoch=now,
    )
    assert dec_legacy.should_trigger is True
    assert dec_legacy.reason is PersonalDreamTriggerReason.ACTIVITY


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

    scan = _make_scan(index_lines=172, file_count=45, earliest_mtime=1000.0, latest_mtime=2000.0)
    decision = _eval(scan, last_reminded_at=1500.0, now_epoch=1500.0 + _SEVEN_DAYS)
    text = format_personal_dream_reminder(
        scan,
        decision,
        index_max_lines=150,
        max_files=40,
        shared_memory_path="/home/zzy/.meiva/vault/memory",
    )
    expected_snippets = (
        "172 lines (soft cap: 150)",
        "45 files (soft cap: 40)",
        "MEMORY.md exceeds 150 lines",
        "note files exceed 40",
        "/home/zzy/.meiva/vault/memory/MEMORY.md",
        "INHERIT.md",
        "inherit/",
        "land/<machine>",
        "#1609",
        "<= 150 lines",
        "\n1. ",
        "\n2. ",
        "\n3. ",
        "\n4. ",
    )
    for snippet in expected_snippets:
        assert snippet in text


def test_read_personal_dream_state_handles_legacy_corrupt_overflow_and_permission_error(
    tmp_path: Path,
) -> None:
    state_file = personal_dream_state_path(tmp_path)

    state_file.write_text('{"last_reminded_at": 1234.5}', encoding="utf-8")
    assert read_personal_dream_state(tmp_path) == PersonalDreamState(
        last_reminded_at=1234.5,
        awaiting_dream_turn=False,
        baseline_mtime=None,
    )

    write_personal_dream_state(
        tmp_path,
        PersonalDreamState(
            last_reminded_at=2000.0, awaiting_dream_turn=True, baseline_mtime=1990.5
        ),
    )
    assert read_personal_dream_state(tmp_path) == PersonalDreamState(
        last_reminded_at=2000.0,
        awaiting_dream_turn=True,
        baseline_mtime=1990.5,
    )

    giant_int = "9" * 400
    state_file.write_text(
        f'{{"last_reminded_at": 1000.0, "awaiting_dream_turn": false, "baseline_mtime": {giant_int}}}',
        encoding="utf-8",
    )
    assert read_personal_dream_state(tmp_path) == PersonalDreamState(
        last_reminded_at=1000.0,
        awaiting_dream_turn=False,
        baseline_mtime=None,
    )

    for raw_text in (
        "{not valid json",
        "[]",
        '{"last_reminded_at": true}',
        '{"last_reminded_at": "1000"}',
        '{"last_reminded_at": NaN}',
        '{"last_reminded_at": Infinity}',
        '{"last_reminded_at": -Infinity}',
        f'{{"last_reminded_at": {giant_int}}}',
    ):
        state_file.write_text(raw_text, encoding="utf-8")
        assert read_personal_dream_state(tmp_path) is None

    state_file.write_bytes(b"\xff\xfe\xfd")
    assert read_personal_dream_state(tmp_path) is None

    state_file.write_text('{"last_reminded_at": 1000.0}', encoding="utf-8")
    locked_dir = personal_memory_path(tmp_path) / "locked"
    _write_file_with_mtime(locked_dir / "note.md", "# Locked\n", 1000.0)
    state_file.chmod(0o000)
    locked_dir.chmod(0o000)
    try:
        assert read_personal_dream_state(tmp_path) is None
        assert scan_personal_memory(tmp_path).file_count == 0
    finally:
        state_file.chmod(0o600)
        locked_dir.chmod(0o700)
