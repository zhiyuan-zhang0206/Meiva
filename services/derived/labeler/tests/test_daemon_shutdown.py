from __future__ import annotations

import sys
from asyncio.constants import THREAD_JOIN_TIMEOUT
from pathlib import Path

from base.host.tests.daemon_shutdown import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)


def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path) -> None:
    assert EXIT_BOUND_S + KILL_SLACK_S < THREAD_JOIN_TIMEOUT / 5
    child = spawn_child(
        tmp_path,
        command=[sys.executable, "-m", "services.derived.labeler.tests.child_shutdown"],
        label="labeler",
    )
    try:
        child.terminate()
        child.wait_bounded_exit(what="wedged executor job")
        assert "[labeler] interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
