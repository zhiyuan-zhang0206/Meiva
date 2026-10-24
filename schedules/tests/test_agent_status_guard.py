# meiva:note
# The guard that stops a stored schedule script before any work when the installed SDK's
# `AgentStatus` lacks a member the script reads. Built-in schedules read statuses through
# `schedules/jobs/agent_directory.py`, which ships with the SDK it runs against; a script that
# reads a member itself declares it here.

from __future__ import annotations

import pytest

from meiva.agents import AgentStatus
from schedules.agent_status_guard import ensure_agent_status_members


def test_missing_member_exits_with_schedule_and_member_names() -> None:
    class DriftedAgentStatus:
        RUNNING = "running"

    with pytest.raises(SystemExit) as raised:
        ensure_agent_status_members(
            DriftedAgentStatus,
            {"IDLING", "RUNNING", "TERMINATED"},
            schedule_name="drift-probe",
        )

    assert raised.value.code == (
        "schedule 'drift-probe' is missing AgentStatus members: IDLING, TERMINATED"
    )


def test_the_installed_status_enum_carries_every_member_the_schedules_read() -> None:
    ensure_agent_status_members(
        AgentStatus, {"IDLING", "RUNNING", "TERMINATED"}, schedule_name="installed-sdk"
    )
