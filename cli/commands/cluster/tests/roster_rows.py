# meiva:note
# A real `MachineStatus` row of the roster wire, with field overrides.
#
# Rows are built from the actual schema, not a hand-written dict, so a renamed or dropped
# field the renderer reads fails the tests instead of drifting silently.

from __future__ import annotations

from datetime import UTC, datetime

from base.api_contracts.status import MachineStatus

__all__ = ["machine"]


def machine(**overrides: object) -> MachineStatus:
    base = MachineStatus(
        name="test-host",
        serve_gateway=True,
        serve_agent_runner=True,
        gateway_url="http://gw:8000",
        up_since_at=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
        online=True,
        paused=False,
    )
    return base.model_copy(update=overrides)
