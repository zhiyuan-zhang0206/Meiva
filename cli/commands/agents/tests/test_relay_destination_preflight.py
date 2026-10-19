from __future__ import annotations

import argparse
from typing import NoReturn
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from base.cluster.machine import MachineIdentity
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

from .. import impersonation_relay as relay
from ..impersonation_adapters import RelayAdapter

_MISSING_ENDPOINT = (
    "No running Antigravity language_server with an HTTP Connect endpoint was found on this machine"
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        lease_id=str(UUID(int=1)),
        agent_id=42,
        session_id=None,
        token_stdin=False,
        provider="antigravity",
        thread_id="24b86dd0-2ddf-42fe-975c-c2770a6580e0",
        codex_remote=None,
        debounce=0,
    )


def test_unresolvable_destination_exits_before_any_lease_work(
    capsys: pytest.CaptureFixture[str],
    *,
    command_machine: MachineIdentity,
) -> None:
    def missing() -> None:
        raise RuntimeError(_MISSING_ENDPOINT)

    def unreachable_database() -> NoReturn:
        pytest.fail("a relay without a destination must not open the database")

    adapter_factory = MagicMock(
        return_value=RelayAdapter(MagicMock(), notify_terminal=False, ready=missing)
    )
    exit_code = relay.cmd_relay(
        _args(),
        database_factory=unreachable_database,
        machine=command_machine,
        adapter_factory=adapter_factory,
    )
    assert exit_code == 1
    assert capsys.readouterr().err == f"Impersonation relay stopped: {_MISSING_ENDPOINT}\n"
