"""Suite-wide health-port guard owned by the CLI probe package."""

import pytest

from base.host.env import port_lease

__all__ = ["guard_health_port_gate"]


@pytest.fixture
def guard_health_port_gate(request: pytest.FixtureRequest) -> None:
    """Autouse safety net: `meiva start`'s pre-bind health-port gate probes leased ports.

    The gate dials this unit's daemon `/healthz` ports before launching (issue
    #977). `tests.fixtures.env_bootstrap` leases every health port in the
    dedicated test band (`base.host.env.port_lease`), so the real gate dials this
    session's own leased ports and never touches production defaults or ephemeral
    OS ports.

    Opt out with `@pytest.mark.real_health_port_gate` when the gate itself is the
    subject (cli/commands/lifecycle/tests/startup/test_start_health_port_gate.py)."""
    if "real_health_port_gate" in request.keywords:
        return
    assert port_lease.leased_ports()
