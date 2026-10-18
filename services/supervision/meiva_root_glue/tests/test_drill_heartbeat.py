from __future__ import annotations

import os
import time
from pathlib import Path

from services.supervision.meiva_root_glue.drill import liveness_probe


class _Tree:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def tree_view(self) -> dict[str, object]:
        return {
            "root_pid": self.pid,
            "units": [{"id": "unit", "state": "running", "pid": self.pid}],
        }


def test_a_missing_beat_leaves_the_liveness_verdict_alone(tmp_path: Path) -> None:
    probe = liveness_probe(_Tree(os.getpid()), "unit", heartbeat_dir=tmp_path)()

    assert probe.alive
    assert "alive" in probe.detail


def test_an_unreadable_beat_is_not_reported_as_a_clean_liveness_pass(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "beats"
    not_a_directory.write_text("", encoding="utf-8")

    probe = liveness_probe(_Tree(os.getpid()), "unit", heartbeat_dir=not_a_directory)()

    assert not probe.alive
    assert "heartbeat" in probe.detail and "unreadable" in probe.detail


def test_a_stale_beat_is_down(tmp_path: Path) -> None:
    beat = tmp_path / "unit.beat"
    beat.write_text("", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(beat, (old, old))

    probe = liveness_probe(_Tree(os.getpid()), "unit", heartbeat_dir=tmp_path)()

    assert not probe.alive
    assert "heartbeat stale" in probe.detail
