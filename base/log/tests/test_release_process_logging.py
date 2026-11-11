from __future__ import annotations

from pathlib import Path

from base.log.process import release_process_logging, start_process_logging


def test_released_process_logging_can_start_again_and_a_second_release_is_a_no_op(
    tmp_path: Path,
) -> None:
    try:
        assert start_process_logging("probe", attach_events=lambda: None, home=tmp_path) is not None
        assert start_process_logging("probe", attach_events=lambda: None, home=tmp_path) is None

        release_process_logging()

        assert start_process_logging("probe", attach_events=lambda: None, home=tmp_path) is not None
    finally:
        release_process_logging()
    release_process_logging()
