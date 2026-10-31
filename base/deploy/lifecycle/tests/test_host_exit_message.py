from __future__ import annotations

from pathlib import Path

import pytest

from base.deploy.lifecycle.host_exit_message import (
    HOST_RESTART_SOURCE,
    PLUGINS_RESTART_MESSAGE,
    clear_host_exit_message,
    consume_host_exit_message,
    write_host_exit_message,
)


def test_host_exit_message_round_trips_and_consumes_once(tmp_path: Path) -> None:
    assert consume_host_exit_message(tmp_path) is None

    write_host_exit_message(tmp_path, "  Zhiyuan rolling out #9999  ")

    assert consume_host_exit_message(tmp_path) == "Zhiyuan rolling out #9999"
    assert consume_host_exit_message(tmp_path) is None


def test_clear_host_exit_message_removes_staged_explanation(tmp_path: Path) -> None:
    write_host_exit_message(tmp_path, "Planned host stop")
    clear_host_exit_message(tmp_path)
    assert consume_host_exit_message(tmp_path) is None
    clear_host_exit_message(tmp_path)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_write_host_exit_message_refuses_blank_text(tmp_path: Path, blank: str) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        write_host_exit_message(tmp_path, blank)


def test_corrupt_host_exit_message_file_degrades_to_none_and_cleans_up(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    marker = run_dir / "host-exit-message.json"
    marker.write_text("{not valid json", encoding="utf-8")

    assert consume_host_exit_message(tmp_path) is None
    assert not marker.exists()


def test_plugins_restart_message_and_source_invariants(tmp_path: Path) -> None:
    assert HOST_RESTART_SOURCE == "system:restart"
    write_host_exit_message(tmp_path, PLUGINS_RESTART_MESSAGE)
    assert consume_host_exit_message(tmp_path) == PLUGINS_RESTART_MESSAGE
