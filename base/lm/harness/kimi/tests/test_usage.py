from __future__ import annotations

import json
from pathlib import Path

from base.lm.harness.kimi.usage import (
    StepUsage,
    UsageSnapshot,
    UsageUnavailableError,
    merge_snapshots,
    read_usage,
    session_log_dir,
    session_log_path,
)

SESSION = "session-1"


def status_update(
    turn: int,
    step: int,
    *,
    other: int = 0,
    out: int = 0,
    read: int = 0,
    write: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "StatusUpdate",
            "data": {
                "turn": turn,
                "step": step,
                "token_usage": {
                    "input_other": other,
                    "output": out,
                    "input_cache_read": read,
                    "input_cache_creation": write,
                },
            },
        }
    )


def write_log(tmp_path: Path, lines: list[str], *, rel: str = "wire.jsonl") -> Path:
    directory = session_log_dir(tmp_path, tmp_path) / SESSION
    path = directory / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_log_path_is_the_documented_layout(tmp_path: Path) -> None:
    path = write_log(tmp_path, [status_update(1, 1)])
    assert session_log_path(tmp_path, tmp_path, SESSION) == path


def test_the_nested_agents_main_wire_log_is_found(tmp_path: Path) -> None:
    path = write_log(tmp_path, [status_update(1, 1)], rel="agents/main/wire.jsonl")
    assert session_log_path(tmp_path, tmp_path, SESSION) == path


def test_a_different_escaping_falls_back_to_the_glob(tmp_path: Path) -> None:
    elsewhere = tmp_path / "sessions" / "-some-other-workspace-" / SESSION
    elsewhere.mkdir(parents=True)
    path = elsewhere / "wire.jsonl"
    path.write_text(status_update(1, 1) + "\n", encoding="utf-8")
    assert session_log_path(tmp_path, tmp_path / "unused", SESSION) == path


def test_a_missing_log_says_usage_is_unavailable(tmp_path: Path) -> None:
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is False
    assert snapshot.steps == ()
    assert snapshot.reason is not None and "no session log" in snapshot.reason


def test_each_status_update_sums_full_prompt_into_input_tokens(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        [
            json.dumps({"type": "TurnBegin", "data": {"text": "hi"}}),
            status_update(1, 1, other=10, out=5, read=100, write=7),
            status_update(1, 2, other=20, out=6, read=200, write=8),
        ],
    )
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is True
    assert snapshot.steps == (
        StepUsage(
            turn=1,
            step=1,
            input_tokens=117,
            output_tokens=5,
            cache_read_tokens=100,
            cache_write_tokens=7,
        ),
        StepUsage(
            turn=1,
            step=2,
            input_tokens=228,
            output_tokens=6,
            cache_read_tokens=200,
            cache_write_tokens=8,
        ),
    )
    assert snapshot.totals().total_tokens == 356


def test_turn_boundaries_advance_turns_without_explicit_numbers(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        [
            json.dumps({"type": "TurnBegin", "payload": {}}),
            json.dumps(
                {
                    "message": {
                        "type": "StatusUpdate",
                        "payload": {"token_usage": {"input_other": 15, "output": 4}},
                    }
                }
            ),
            json.dumps({"type": "TurnBegin", "payload": {}}),
            json.dumps(
                {
                    "message": {
                        "type": "StatusUpdate",
                        "payload": {"token_usage": {"input_other": 25, "output": 9}},
                    }
                }
            ),
        ],
    )
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert [step.input_tokens for step in snapshot.for_turn(1)] == [15]
    assert [step.input_tokens for step in snapshot.latest_turn()] == [25]


def test_a_log_without_assistant_usage_says_so(tmp_path: Path) -> None:
    write_log(tmp_path, [json.dumps({"type": "TurnBegin", "data": {}})])
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is False
    assert snapshot.reason is not None and "no assistant usage" in snapshot.reason


def test_a_broken_line_says_so_instead_of_reading_as_zero(tmp_path: Path) -> None:
    write_log(tmp_path, [status_update(1, 1), "{not json"])
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is False
    assert snapshot.reason is not None and "line 2 is not JSON" in snapshot.reason


def test_a_decoder_failure_travels_with_the_snapshot(tmp_path: Path) -> None:
    write_log(tmp_path, [status_update(1, 1)])

    def refuse(path: Path) -> bytes:
        raise UsageUnavailableError(f"{path} is unreadable")

    snapshot = read_usage(tmp_path, tmp_path, SESSION, decoder=refuse)
    assert snapshot.per_turn_usage is False
    assert snapshot.reason is not None and "is unreadable" in snapshot.reason


def test_a_boolean_or_missing_counter_reads_as_zero(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "StatusUpdate",
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "token_usage": {"input_other": True, "output": 3},
                    },
                }
            )
        ],
    )
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is True
    assert snapshot.steps[0].input_tokens == 0
    assert snapshot.steps[0].output_tokens == 3


def test_merge_marks_the_whole_snapshot_unavailable() -> None:
    measured = UsageSnapshot(
        per_turn_usage=True,
        session_id=SESSION,
        steps=(
            StepUsage(
                turn=1,
                step=1,
                input_tokens=4,
                output_tokens=2,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
        ),
    )
    unmeasured = UsageSnapshot(per_turn_usage=False, session_id=SESSION, reason="no log")
    merged = merge_snapshots([measured, unmeasured])
    assert merged.per_turn_usage is False
    assert merged.reason == "no log"
    assert merged.steps == measured.steps
    assert merge_snapshots([measured]).per_turn_usage is True
