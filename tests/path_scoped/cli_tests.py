# meiva:note
# Shared fixtures for the CLI tests (registered by `tests/fixtures/path_scopes.py`).
#
# Production service and OS job boundaries remain guarded by the root fixtures.

from __future__ import annotations

import pytest

from base.cluster.machine.tests.identity import as_machine as as_machine
from base.sessions.pty.tests.pty_reaper import pty_reaper as pty_reaper
from cli.commands.tests.health_port_guard import guard_health_port_gate as guard_health_port_gate
from cli.database.tests.cli_tests import (
    cli_log_sinks as cli_log_sinks,
)
from cli.database.tests.cli_tests import (
    installed_machine_identity as installed_machine_identity,
)
from cli.database.tests.cli_tests import (
    operator_database as operator_database,
)
from cli.database.tests.cli_tests import (
    operator_pipeline as operator_pipeline,
)
from cli.database.tests.command_inputs import command_runtime as command_runtime


@pytest.fixture(autouse=True)
def _cli_orchestration_guards(
    guard_health_port_gate: None,
    guard_service_readiness: None,
    homes_get_their_own_ports: None,
    permissions_helper_native_io: None,
) -> None:
    """Protect the same CLI effects as the package-local CLI test owner."""
