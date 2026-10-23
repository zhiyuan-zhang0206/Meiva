from __future__ import annotations

import pytest

from base.agents.birth.harness import LAUNCHABLE_HARNESSES
from base.api_contracts.agent_contract import Harness
from gateway.cluster.harness_rows import availability_rows, launchable
from ops.rpc_schemas import HarnessProbeResult, HarnessProbeRow

_PROBED = [
    {
        "harness": "meiva",
        "status": "available",
        "reason": "built-in runtime; needs no external CLI or login",
    },
    {"harness": "claude_code", "status": "available", "reason": "signed-in account recorded"},
    {"harness": "codex", "status": "unknown", "reason": "installed; login state unknown"},
    {"harness": "antigravity", "status": "unavailable", "reason": "not installed"},
]


# meiva:note
# Installed-but-unstartable must not read as a working choice, and a harness without
# a launcher must not read as launchable either. Which harnesses have a launcher is
# read from LAUNCHABLE_HARNESSES rather than pinned to one that cannot launch today,
# so adding an adapter does not turn this red and removing one does. The built-in
# runtime is the exception the rule names: it needs neither an install nor a login,
# so its probe row says nothing about whether it can run.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
@pytest.mark.parametrize("harness", list(Harness))
def test_launchable_needs_a_launcher_and_a_machine_that_can_run_it(harness: Harness) -> None:
    available = HarnessProbeRow(harness=harness, status="available", reason="signed in")
    assert launchable(available, LAUNCHABLE_HARNESSES) is (harness in LAUNCHABLE_HARNESSES)

    if harness is Harness.MEIVA:
        return

    unknown = HarnessProbeRow(harness=harness, status="unknown", reason="login unknown")
    missing = HarnessProbeRow(harness=harness, status="unavailable", reason="not installed")
    assert launchable(unknown, LAUNCHABLE_HARNESSES) is False
    assert launchable(missing, LAUNCHABLE_HARNESSES) is False


def test_a_harness_the_build_cannot_launch_is_not_launchable_even_when_available() -> None:
    available = HarnessProbeRow(harness=Harness.CLAUDE_CODE, status="available", reason="ok")

    assert launchable(available, frozenset()) is False
    assert launchable(available, frozenset({Harness.CLAUDE_CODE})) is True


def test_rows_carry_status_and_reason_through() -> None:
    probed = availability_rows(
        HarnessProbeResult(
            machine="remote-host",
            harnesses=[HarnessProbeRow.model_validate(row) for row in _PROBED],
        ),
        LAUNCHABLE_HARNESSES,
    )
    assert [row.status for row in probed] == ["available", "available", "unknown", "unavailable"]
    assert [row.reason for row in probed] == [row["reason"] for row in _PROBED]
    # meiva:note
    # Billing travels with the harness, not with the model or the route: the
    # built-in runtime is metered, the external CLIs run on subscriptions, and the
    # spawn picker shows rates only for the metered one (user 2026-10-20).
    # meiva:link base/agents/docs/harness/harness.meiva.okf.md
    assert [row.billing for row in probed] == [
        "api",
        "subscription",
        "subscription",
        "subscription",
    ]
