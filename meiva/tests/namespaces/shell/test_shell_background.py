# meiva:note
# `meiva.shell.run_background`: the SDK entry's argument checks and its wiring to the agent's workspace, session and log.  The line a run executes, its log ring and the end-to-end behaviour of a run (notice, close, keep, failures) are the implementation's, tested in `meiva/shell/tests` against a real pty-sessions service with a recording CLI. Here the entry runs with the real `meiva` CLI beside the interpreter.
import time
from pathlib import Path

import pytest

import meiva
from base.paths import workspace_dir

pytestmark = [
    pytest.mark.usefixtures("sdk_model_owner", "pty_service", "_isolated_agent"),
]


def test_run_background_rejects_empty_cmd() -> None:
    with pytest.raises(ValueError, match="cmd cannot be empty"):
        meiva.shell.run_background("   ", name="test-empty", ttl=120)


def test_run_background_rejects_unknown_notify() -> None:
    with pytest.raises(ValueError, match="notify must be one of"):
        meiva.shell.run_background("echo hi", name="test-notify", ttl=120, notify="success")


def test_run_background_requires_name() -> None:
    with pytest.raises(TypeError):
        meiva.shell.run_background("echo hi")  # type: ignore[call-arg]


@pytest.mark.flaky
def test_run_background_runs_in_the_workspace_and_logs_there(_agent_row: int) -> None:
    handle = meiva.shell.run_background(
        "echo facade-ok; pwd", name="test-facade", keep=True, ttl=120
    )
    try:
        workspace = workspace_dir(_agent_row)
        log_path = Path(handle.output_path)
        assert log_path == workspace / ".shell_logs" / f"{handle.session_id}_test-facade.log"
        deadline = time.monotonic() + 15
        log = ""
        while time.monotonic() < deadline and "[exit 0]" not in log:
            time.sleep(0.1)
            log = log_path.read_text()
        assert "facade-ok" in log, log
        assert str(workspace.resolve()) in log or str(workspace) in log, log
        assert log.strip().endswith("[exit 0]"), log
        assert meiva.shell.sessions.list()[handle.session_id] == "test-facade"
    finally:
        meiva.shell.sessions.kill(handle)
