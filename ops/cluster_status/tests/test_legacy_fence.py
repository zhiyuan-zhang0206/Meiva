from pathlib import Path

import pytest

from base.sessions.pty import client, terminal_fence
from base.sessions.pty.tests.legacy_service import legacy_service
from ops.cluster_status import kill_agent_shells


def test_legacy_fence_falls_back_and_retains_cutoff(unit_home: Path) -> None:
    with legacy_service("unknown_method: fence_agent_shells") as calls:
        cleanup = kill_agent_shells(7, before_session_index=10, home=unit_home)
    assert cleanup.killed == [1]
    assert not cleanup.fenced
    assert calls == ["fence_agent_shells", "list", "list", "kill"]
    assert terminal_fence.read(unit_home).cutoffs == {7: 10}


@pytest.mark.parametrize("error", ["fence failed", "unknown_method: kill"])
def test_other_service_errors_propagate(unit_home: Path, error: str) -> None:
    with legacy_service(error) as calls, pytest.raises(client.ServiceError, match=error):
        kill_agent_shells(7, before_session_index=10, home=unit_home)
    assert calls == ["fence_agent_shells"]
