# meiva:note
# The API reads a schedule session's output straight from the real session service.

from __future__ import annotations

from pathlib import Path

from base.cluster import session_name
from base.host.env.registry import process_environment
from base.sessions.backend import get_shell_backend
from base.sessions.env_forwarding import forward_env_dict
from base.sessions.pty.tests.pty_service import PtyServiceProcess
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_shells import wait_for
from gateway.schedules import session_control


def test_capture_returns_none_without_a_session(pty_service: PtyServiceProcess) -> None:
    assert session_control.capture_blocking(get_shell_backend(), 123, 200) is None


def test_capture_reads_the_live_sessions_pane(
    pty_service: PtyServiceProcess, tmp_path: Path
) -> None:
    backend = get_shell_backend()
    backend.new_session(
        session_name("schedule-123"),
        "echo SCHEDULE_OUTPUT; sleep 30",
        tmp_path,
        env=forward_env_dict(process_environment()),
    )

    def captured() -> bool:
        return "SCHEDULE_OUTPUT" in (session_control.capture_blocking(backend, 123, 50) or "")

    assert wait_for(captured)
