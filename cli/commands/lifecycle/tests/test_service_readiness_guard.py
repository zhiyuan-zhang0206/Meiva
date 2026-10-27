# meiva:note
# The service-readiness guard `cli/conftest.py` binds for every CLI test holds, and its
# opt-out marker still reaches the real wait. The guard lives in `cli/tests/guards/`, beside the
# conftest that binds it; this file pins its contract where the guarded wait lives.

from __future__ import annotations

from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from cli.commands.lifecycle import root_readiness
from cli.commands.probe import ReadinessWait


def test_service_readiness_wait_reports_every_service_ready(tmp_path: Path) -> None:
    result = root_readiness.wait_for_service_tree(
        (), timeout_s=180.0, home=tmp_path, timers=SYSTEM_TIMERS
    )

    assert result == ReadinessWait((), 0.0, sessions_gone=False)
    assert (
        root_readiness.wait_for_service_tree.__module__
        == "cli.tests.guards.service_readiness_guard"
    )


@pytest.mark.real_service_readiness_gate
def test_service_readiness_marker_reaches_the_real_wait() -> None:
    assert (
        root_readiness.wait_for_service_tree.__module__ == "cli.commands.lifecycle.root_readiness"
    )
