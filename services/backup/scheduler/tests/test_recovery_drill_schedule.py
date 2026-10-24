from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.backup.scheduler.recovery_drill import (
    load_local_dump_restore_success,
    record_local_dump_restore_success,
)

_DONE = datetime(2026, 8, 30, 3, 0, 5, tzinfo=UTC)


def test_no_marker_means_no_recorded_success(tmp_path: Path) -> None:
    assert load_local_dump_restore_success(tmp_path / "marker.json") is None


def test_a_recorded_success_reads_back_as_the_same_instant_in_utc(tmp_path: Path) -> None:
    marker = tmp_path / "db" / "marker.json"

    record_local_dump_restore_success(_DONE, marker)

    assert load_local_dump_restore_success(marker) == _DONE
    assert marker.stat().st_mode & 0o777 == 0o600


def test_a_later_success_replaces_the_earlier_one(tmp_path: Path) -> None:
    marker = tmp_path / "marker.json"
    record_local_dump_restore_success(datetime(2026, 8, 23, 3, tzinfo=UTC), marker)

    record_local_dump_restore_success(_DONE, marker)

    assert load_local_dump_restore_success(marker) == _DONE


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"completed_at": 3}', '{"completed_at": "2026-08-30T03:00:00"}', "{}"],
)
def test_a_marker_of_another_shape_is_invalid(tmp_path: Path, content: str) -> None:
    marker = tmp_path / "marker.json"
    marker.write_text(content)

    with pytest.raises(RuntimeError, match="success marker is invalid"):
        load_local_dump_restore_success(marker)


def test_a_naive_success_time_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="TZ-aware"):
        record_local_dump_restore_success(
            datetime.fromisoformat("2026-08-30T03:00:00"), tmp_path / "marker.json"
        )
