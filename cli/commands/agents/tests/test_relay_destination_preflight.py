from __future__ import annotations

import argparse
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from base.agents.impersonation import host_transport
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


def test_relay_reads_the_request_record_by_path(tmp_path: Path) -> None:
    thread_id = "24b86dd0-2ddf-42fe-975c-c2770a6580e0"
    csrf = "recorded-csrf"
    server = host_transport.AntigravityServer(address="127.0.0.1:59901", csrf_token=csrf)
    record = host_transport.write_antigravity_endpoint(server, thread_id=thread_id, home=tmp_path)
    destination = relay.antigravity_destination(
        argparse.Namespace(thread_id=thread_id, antigravity_endpoint_file=str(record))
    )
    assert destination() == server


def test_relay_names_a_missing_request_record(tmp_path: Path) -> None:
    absent = tmp_path / "absent.json"
    destination = relay.antigravity_destination(
        argparse.Namespace(
            thread_id="24b86dd0-2ddf-42fe-975c-c2770a6580e0",
            antigravity_endpoint_file=str(absent),
        )
    )
    with pytest.raises(RuntimeError, match="missing at"):
        destination()
