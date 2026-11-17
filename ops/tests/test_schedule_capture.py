from pathlib import Path

import pytest

from ops.cluster.operations import schedule_capture_op


@pytest.mark.parametrize("owner", [None, 42])
def test_schedule_capture_reads_latest_log_in_owner_locality(
    tmp_path: Path, owner: int | None
) -> None:
    root = tmp_path if owner is None else tmp_path / "workspaces" / str(owner)
    workdir = root / "schedules" / "7"
    workdir.mkdir(parents=True)
    (workdir / "latest.log").write_text("first\nsecond\nthird\n")

    result = schedule_capture_op(7, 2, home=tmp_path, owner_agent_id=owner)

    assert result.transcript == ["second", "third"]
    assert result.captured is None


def test_schedule_capture_without_output_is_empty(tmp_path: Path) -> None:
    result = schedule_capture_op(7, home=tmp_path)

    assert result.transcript is None
    assert result.captured is None
