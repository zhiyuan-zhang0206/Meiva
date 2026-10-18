import datetime
import pathlib

import pytest

import meiva
from meiva import watcher
from meiva.shell import SessionHandle

pytestmark = [
    pytest.mark.usefixtures("sdk_model_owner", "pty_service", "_isolated_agent"),
]


def _is_live_watcher(wid: int, name: str) -> bool:
    return meiva.shell.sessions.list().get(wid) == name


def test_launch_returns_the_same_handle_as_run_background(_agent_row: int) -> None:
    handle = watcher.launch("import time\ntime.sleep(60)\n", timeout="1h", name="test-handle")
    try:
        assert isinstance(handle, SessionHandle)
        assert isinstance(handle.session_id, int)
        assert handle.output_path.endswith(".log")
        assert pathlib.Path(handle.output_path).exists()
        assert _is_live_watcher(handle.session_id, "test-handle")
    finally:
        meiva.shell.sessions.kill(handle.session_id)


def test_at_returns_the_same_handle_as_run_background(_agent_row: int) -> None:
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    handle = watcher.at(when, "wake", name="test-at-handle")
    try:
        assert isinstance(handle, SessionHandle)
        assert isinstance(handle.session_id, int)
        assert pathlib.Path(handle.output_path).exists()
        assert _is_live_watcher(handle.session_id, "test-at-handle")
    finally:
        meiva.shell.sessions.kill(handle.session_id)


def test_cron_returns_the_same_handle_as_run_background(_agent_row: int) -> None:
    handle = watcher.cron("0 4 * * *", "wake", name="test-cron-handle")
    try:
        assert isinstance(handle, SessionHandle)
        assert isinstance(handle.session_id, int)
        assert pathlib.Path(handle.output_path).exists()
        assert _is_live_watcher(handle.session_id, "test-cron-handle")
    finally:
        meiva.shell.sessions.kill(handle.session_id)
