from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.lm.harness.antigravity.timing import duration_ms, timing_key
from base.lm.harness.antigravity.timing_store import load_timings, store_timings, timing_path
from base.lm.harness.antigravity.trajectory import parse_trajectory
from base.lm.harness.antigravity.transcript.file import AntigravityRecordFile
from base.lm.harness.antigravity.transcript.window import EntryParser
from base.lm.harness.session.transcript.entries import Reasoning

STAMP = "2026-11-17T05:30:00Z"


def snapshot(duration: object = "30.265829s", *, status: str = "DONE", total: int = 1):
    return parse_trajectory(
        {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": total,
            "trajectory": {
                "steps": [
                    {
                        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                        "status": f"CORTEX_STEP_STATUS_{status}",
                        "metadata": {"createdAt": "2026-11-17T05:30:00.123456789Z"},
                        "plannerResponse": {
                            "thinking": "private thought",
                            "thinkingDuration": duration,
                        },
                    }
                ]
            },
        }
    )


def transcript(path: Path, *, thought: str = "private thought", index: int = 0) -> bytes:
    text = (
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "step_index": index,
                "created_at": STAMP,
                "thinking": thought,
                "content": "answer",
            }
        )
        + "\n"
    )
    path.write_text(text)
    return text.encode()


def measured(path: Path) -> list[int | None]:
    rows = AntigravityRecordFile(path).rows_after(0).rows
    return [
        entry.duration_ms for entry in EntryParser().entries(rows) if isinstance(entry, Reasoning)
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30.265829s", 30265),
        ("0s", 0),
        ("0.000001s", 0),
        (None, None),
        (True, None),
        (1, None),
        ("NaNs", None),
        ("infs", None),
        ("-1s", None),
        ("1ms", None),
        ("1e5s", None),
        ("9" * 30 + "s", None),
    ],
)
def test_units_are_finite_and_explicit(raw: object, expected: int | None) -> None:
    assert duration_ms(raw) == expected


def test_unique_identity_and_offline_restart(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    original = transcript(path)
    assert measured(path) == [None]
    store_timings(path, snapshot())
    assert measured(path) == [30265]
    revision = AntigravityRecordFile(path).revision()
    store_timings(path, snapshot())
    assert AntigravityRecordFile(path).revision() == revision
    assert path.read_bytes() == original
    assert "private thought" not in timing_path(path).read_text()
    assert timing_key(0, STAMP, "private thought") == timing_key(
        0, "2026-11-17T05:30:00.999Z", "private thought"
    )


def test_zero_missing_and_live_native_fields(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    transcript(path)
    store_timings(path, snapshot("0s", status="GENERATING"))
    assert measured(path) == [None]
    store_timings(path, snapshot("0s"))
    assert measured(path) == [0]


def test_older_snapshot_cannot_replace_verified_fact(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    transcript(path)
    store_timings(path, snapshot(total=2))
    before = timing_path(path).read_bytes()
    store_timings(path, snapshot("1s", total=1))
    assert timing_path(path).read_bytes() == before
    assert measured(path) == [30265]


def test_duplicate_raw_identity_is_unknown_and_recorded_conflict_stays_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "transcript_full.jsonl"
    raw = transcript(path)
    store_timings(path, snapshot())
    with path.open("ab") as handle:
        handle.write(raw)
    store_timings(path, snapshot(total=2))
    assert measured(path) == [None, None]
    transcript(path)
    store_timings(path, snapshot())
    store_timings(path, snapshot("1s"))
    assert measured(path) == [None]
    store_timings(path, snapshot())
    assert measured(path) == [None]


def test_rewritten_offsets_and_corrupt_or_cleaned_sidecar_are_unknown(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    transcript(path)
    store_timings(path, snapshot())
    transcript(path, thought="different thought with same byte-offset")
    assert measured(path) == [None]
    timing_path(path).write_text("corrupt")
    assert load_timings(path) is None
    assert measured(path) == [None]
    transcript(path)
    store_timings(path, snapshot())
    assert measured(path) == [30265]
    timing_path(path).unlink()
    assert measured(path) == [None]
    store_timings(path, snapshot())
    assert measured(path) == [30265]
    assert not list(tmp_path.glob("*.tmp"))


def test_write_failure_preserves_original_record(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    raw = transcript(path)
    timing_path(path).mkdir()
    store_timings(path, snapshot())
    assert path.read_bytes() == raw
    assert measured(path) == [None]


def test_concurrent_snapshot_writers_are_atomic_and_keep_the_newer_watermark(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "transcript_full.jsonl"
    transcript(path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(store_timings, path, snapshot(total=n)) for n in [2, 1, 2, 1]]
        for future in futures:
            future.result()
    saved = load_timings(path)
    assert saved is not None and saved.total == 2
    assert measured(path) == [30265]
    assert not list(tmp_path.glob("*.tmp"))


def test_growth_is_unknown_until_revalidated_and_stale_snapshot_still_detects_ambiguity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "transcript_full.jsonl"
    raw = transcript(path)
    store_timings(path, snapshot(total=2))
    with path.open("ab") as handle:
        handle.write(raw)
    assert measured(path) == [None, None]
    store_timings(path, snapshot(total=1))
    saved = load_timings(path)
    assert saved is not None and saved.total == 2
    assert measured(path) == [None, None]


def test_invalid_sidecar_version_and_values_do_not_block_record_reads(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    transcript(path)
    store_timings(path, snapshot())
    data = json.loads(timing_path(path).read_text())
    data["version"] = 2
    timing_path(path).write_text(json.dumps(data))
    assert measured(path) == [None]
    store_timings(path, snapshot())
    data = json.loads(timing_path(path).read_text())
    data["entries"]["0"]["ms"] = True
    timing_path(path).write_text(json.dumps(data))
    assert measured(path) == [None]


def test_duplicate_sidecar_offsets_and_fingerprints_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    transcript(path)
    store_timings(path, snapshot())
    data = json.loads(timing_path(path).read_text())
    value = json.dumps(data["entries"]["0"])
    prefix = json.dumps({key: val for key, val in data.items() if key != "entries"})[:-1]
    timing_path(path).write_text(prefix + ', "entries": {"0":' + value + ', "0":' + value + "}}")
    assert measured(path) == [None]
    store_timings(path, snapshot())
    data = json.loads(timing_path(path).read_text())
    data["entries"]["1"] = data["entries"]["0"]
    timing_path(path).write_text(json.dumps(data))
    assert measured(path) == [None]


def test_same_size_record_rewrite_invalidates_verified_offsets(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    before = transcript(path)
    store_timings(path, snapshot())
    transcript(path, thought="another thought")
    assert path.stat().st_size == len(before)
    assert measured(path) == [None]
