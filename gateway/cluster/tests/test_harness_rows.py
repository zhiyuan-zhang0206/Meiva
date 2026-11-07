from __future__ import annotations

import pytest

from base.agents.birth.harness import LAUNCHABLE_HARNESSES
from base.api_contracts.agent_contract import Harness
from gateway.cluster.harness_rows import availability_rows, launchable
from ops.rpc_schemas import HarnessModelOption, HarnessProbeResult, HarnessProbeRow

_MODEL = HarnessModelOption(
    id="claude-sonnet-5-5",
    reasoning_effort_options=["low", "medium", "high"],
    reasoning_effort_default="high",
)

_PROBED = [
    {
        "harness": "meiva",
        "status": "available",
        "reason": "built-in runtime; needs no external CLI or login",
    },
    {
        "harness": "claude_code",
        "status": "available",
        "reason": "signed-in account recorded",
        "models": [
            {
                "id": "claude-sonnet-5-5",
                "reasoning_effort_options": ["low", "medium", "high"],
                "reasoning_effort_default": "high",
            }
        ],
    },
    {"harness": "codex", "status": "unknown", "reason": "installed; login state unknown"},
    {"harness": "antigravity", "status": "unavailable", "reason": "not installed"},
]


# meiva:note
# Installed-but-unstartable must not read as a working choice, and a harness without
# a launcher or without probed models must not read as launchable either. Which
# harnesses have a launcher is read from LAUNCHABLE_HARNESSES rather than pinned to
# one that cannot launch today, so adding an adapter does not turn this red and
# removing one does. The built-in runtime is the exception the rule names: it needs
# neither an install, a login, nor a CLI model probe.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
@pytest.mark.parametrize("harness", list(Harness))
def test_launchable_needs_a_launcher_and_a_machine_that_can_run_it(harness: Harness) -> None:
    models = [] if harness is Harness.MEIVA else [_MODEL]
    available = HarnessProbeRow(
        harness=harness, status="available", reason="signed in", models=models
    )
    assert launchable(available, LAUNCHABLE_HARNESSES) is (harness in LAUNCHABLE_HARNESSES)

    if harness is Harness.MEIVA:
        return

    model_less = HarnessProbeRow(harness=harness, status="available", reason="signed in")
    unknown = HarnessProbeRow(harness=harness, status="unknown", reason="login unknown")
    missing = HarnessProbeRow(harness=harness, status="unavailable", reason="not installed")
    assert launchable(model_less, LAUNCHABLE_HARNESSES) is False
    assert launchable(unknown, LAUNCHABLE_HARNESSES) is False
    assert launchable(missing, LAUNCHABLE_HARNESSES) is False


def test_a_harness_the_build_cannot_launch_is_not_launchable_even_when_available() -> None:
    available = HarnessProbeRow(
        harness=Harness.CLAUDE_CODE, status="available", reason="ok", models=[_MODEL]
    )

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


def test_rows_carry_probed_models_through() -> None:
    probed = availability_rows(
        HarnessProbeResult(
            machine="remote-host",
            harnesses=[HarnessProbeRow.model_validate(row) for row in _PROBED],
        ),
        LAUNCHABLE_HARNESSES,
    )
    assert probed[0].models == []
    assert [m.id for m in probed[1].models] == ["claude-sonnet-5-5"]
