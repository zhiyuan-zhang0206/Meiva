"""CLI orchestration guards apply beside their actual consumers."""

import pytest

from cli.commands.tests.health_port_guard import guard_health_port_gate as guard_health_port_gate
from cli.tests.guards.service_readiness_guard import (
    guard_service_readiness as guard_service_readiness,
)


@pytest.fixture(autouse=True)
def _cli_orchestration_guards(
    guard_health_port_gate: None,
    guard_service_readiness: None,
    homes_get_their_own_ports: None,
) -> None:
    """Admit CLI logic while keeping probes off the host."""
