from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.lm.harness.deepseek.usage import (
    StepUsage,
    UsageSnapshot,
    UsageUnavailableError,
    decompress,
    merge_snapshots,
    read_usage,
    session_log_dir,
    session_log_path,
)

SESSION = "session-1"


def assistant_message(
    turn: int, step: int, *, text: int = 0, out: int = 0, read: int = 0, write: int = 0
) -> str:
    return json.dumps(
        {
            "type": "assistant/message",
            "data": {
                "turn": turn,
                "step": step,
                "usage": {
                    "inputTokens": text,
                    "outputTokens": out,
                    "cacheReadTokens": read,
                    "cacheWriteTokens": write,
                },
            },
        }
    )


def write_log(tmp_path: Path, lines: list[str], *, name: str = "session.v4.jsonl") -> Path:
    directory = session_log_dir(tmp_path, tmp_path) / SESSION
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_log_path_is_the_documented_layout(tmp_path: Path) -> None:
    path = write_log(tmp_path, [assistant_message(1, 1)])
    assert session_log_path(tmp_path, tmp_path, SESSION) == path


def test_the_compressed_log_wins_over_the_plain_one(tmp_path: Path) -> None:
    write_log(tmp_path, [])
    compressed = write_log(tmp_path, [], name="session.v4.jsonl.zstd")
    assert session_log_path(tmp_path, tmp_path, SESSION) == compressed


def test_a_different_escaping_falls_back_to_the_glob(tmp_path: Path) -> None:
    elsewhere = tmp_path / "sessions" / "-some-other-workspace-" / SESSION
    elsewhere.mkdir(parents=True)
    path = elsewhere / "session.v4.jsonl"
    path.write_text(assistant_message(1, 1) + "\n", encoding="utf-8")
    assert session_log_path(tmp_path, tmp_path / "unused", SESSION) == path


def test_a_missing_log_says_usage_is_unavailable(tmp_path: Path) -> None:
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is False
    assert snapshot.steps == ()
    assert snapshot.reason is not None and "no session log" in snapshot.reason


def test_each_assistant_message_is_one_step(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        [
            json.dumps({"type": "user/message", "data": {"text": "hi"}}),
            assistant_message(1, 1, text=10, out=5, read=100, write=7),
            assistant_message(1, 2, text=20, out=6, read=200, write=8),
        ],
    )
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is True
    assert snapshot.steps == (
        StepUsage(
            turn=1,
            step=1,
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=100,
            cache_write_tokens=7,
        ),
        StepUsage(
            turn=1,
            step=2,
            input_tokens=20,
            output_tokens=6,
            cache_read_tokens=200,
            cache_write_tokens=8,
        ),
    )
    assert snapshot.totals().total_tokens == 356


def test_the_latest_turn_is_the_highest_turn_number(tmp_path: Path) -> None:
    write_log(tmp_path, [assistant_message(1, 1, text=1), assistant_message(2, 1, text=2)])
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert [step.input_tokens for step in snapshot.latest_turn()] == [2]
    assert [step.input_tokens for step in snapshot.for_turn(1)] == [1]


def test_a_log_without_assistant_usage_says_so(tmp_path: Path) -> None:
    write_log(tmp_path, [json.dumps({"type": "user/message", "data": {}})])
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is False
    assert snapshot.reason is not None and "no assistant usage" in snapshot.reason


def test_a_broken_line_says_so_instead_of_reading_as_zero(tmp_path: Path) -> None:
    write_log(tmp_path, [assistant_message(1, 1), "{not json"])
    snapshot = read_usage(tmp_path, tmp_path, SESSION)
    assert snapshot.per_turn_usage is False
    assert snapshot.reason is not None and "line 2 is not JSON" in snapshot.reason


def test_a_decoder_failure_travels_with_the_snapshot(tmp_path: Path) -> None:
    write_log(tmp_path, [assistant_message(1, 1)])

    def refuse(path: Path) -> bytes:
        raise UsageUnavailableError(f"{path} is not a readable zstd frame")

    snapshot = read_usage(tmp_path, tmp_path, SESSION, decoder=refuse)
    assert snapshot.per_turn_usage is False
    assert snapshot.reason is not None and "not a readable zstd frame" in snapshot.reason


def test_a_boolean_or_missing_counter_reads_as_zero(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "assistant/message",
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "usage": {"inputTokens": True, "outputTokens": 3},
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


def test_a_zstd_frame_is_decompressed(tmp_path: Path) -> None:
    zstandard = pytest.importorskip("zstandard")
    path = tmp_path / "session.v4.jsonl.zstd"
    compressed = zstandard.ZstdCompressor().compress(assistant_message(1, 1, text=9).encode())
    path.write_bytes(compressed)
    assert decompress(path) == assistant_message(1, 1, text=9).encode()


def test_a_plain_file_is_read_without_decompression(tmp_path: Path) -> None:
    path = tmp_path / "session.v4.jsonl"
    path.write_text("{}", encoding="utf-8")
    assert decompress(path) == b"{}"
