# meiva:note
# `cmd_relay` and the relay's reads against real resources: the command runs a relay for a real
# lease in the throwaway database and ends the way the lease or the host ends it, and a read of
# the lease shows each status the shared enum names. The relay's own loop is in
# `test_relay_shell.py`.

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import relay as lease_relay
from base.agents.impersonation.status import ImpersonationStatus
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.events.live.bus import EventBus
from cli.commands.agents.impersonation_relay import (
    RelayInputs,
    cmd_relay,
    read_relay_inbox,
    write_heartbeat,
)
from cli.commands.agents.tests.lease_support import (
    START_MESSAGE,
    ActiveSession,
    LeaseAt,
    assert_message_pending,
    delivery_attempts,
)
from cli.commands.agents.tests.lease_support import active as active
from cli.commands.agents.tests.lease_support import lease_at as lease_at
from cli.commands.agents.tests.loopback_hosts import codex_app_server, short_socket_path
from cli.commands.agents.tests.relay_harness import THREAD
from cli.database import OperatorDatabaseFactory
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

UNMINTED = "not-the-relay-token"


def _relay_args(lease: dict[str, Any], **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "lease_id": lease["id"],
        "agent_id": lease["agent_id"],
        "session_id": None,
        "provider": "claude",
        "thread_id": None,
        "codex_remote": None,
        "antigravity_endpoint_file": None,
        "debounce": 0.0,
    }
    return argparse.Namespace(**{**values, **overrides})


def test_the_relay_command_runs_a_claude_relay_to_the_leases_expiry(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    redis_url: str,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    stdout = io.StringIO()
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '6 seconds' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    code = cmd_relay(
        _relay_args(lease),
        inputs=RelayInputs(lease["relay_token"], redis_url, codex_home=None, stdout=stdout),
        database_factory=operator_database,
        machine=machine_identity,
    )
    out = stdout.getvalue()
    assert code == 0
    assert capsys.readouterr().err == ""
    assert out.startswith(START_MESSAGE)
    assert f"[id={mid}] kind=chat from=user" in out
    assert "ended: expired" in out
    assert out.index("[id=") < out.index("ended: expired")
    assert delivery_attempts(db_conn, mid) == 1


def test_the_relay_command_reports_a_host_that_refuses_to_be_steered(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    redis_url: str,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    stdout = io.StringIO()
    with short_socket_path() as socket_path, codex_app_server(socket_path, [False]) as host:
        code = cmd_relay(
            _relay_args(lease, provider="codex", thread_id=str(THREAD), codex_remote=host.endpoint),
            inputs=RelayInputs(lease["relay_token"], redis_url, codex_home=None, stdout=stdout),
            database_factory=operator_database,
            machine=machine_identity,
        )
    assert code == 1
    assert capsys.readouterr().err == (
        "Impersonation relay stopped: Codex Steer delivery failed: "
        "turn/start refused (-32603: ActiveTurnNotSteerable)\n"
    )
    assert_message_pending(db_conn, mid)


def test_unresolvable_destination_exits_before_any_lease_work(
    active: ActiveSession,
    tmp_path: Path,
    redis_url: str,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    stdout = io.StringIO()
    absent = tmp_path / "absent.json"
    code = cmd_relay(
        _relay_args(
            lease,
            provider="antigravity",
            thread_id=str(THREAD),
            antigravity_endpoint_file=str(absent),
        ),
        inputs=RelayInputs(lease["relay_token"], redis_url, codex_home=None, stdout=stdout),
        database_factory=operator_database,
        machine=machine_identity,
    )
    assert code == 1
    assert capsys.readouterr().err == (
        f"Impersonation relay stopped: the request's Antigravity endpoint record is missing at {absent}\n"
    )


def test_agent_mismatch_refuses_inbox_before_subscription(
    active: ActiveSession,
    redis_url: str,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    stdout = io.StringIO()
    code = cmd_relay(
        _relay_args(lease, agent_id=lease["agent_id"] + 1),
        inputs=RelayInputs(lease["relay_token"], redis_url, codex_home=None, stdout=stdout),
        database_factory=operator_database,
        machine=machine_identity,
    )
    assert code == 1
    assert "does not belong to the requested agent" in capsys.readouterr().err


def test_the_relay_command_refuses_a_credential_the_lease_did_not_mint(
    active: ActiveSession,
    redis_url: str,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    code = cmd_relay(
        _relay_args(lease),
        inputs=RelayInputs(UNMINTED, redis_url, codex_home=None, stdout=io.StringIO()),
        database_factory=operator_database,
        machine=machine_identity,
    )
    assert code == 1
    assert capsys.readouterr().err == "Impersonation relay stopped: Invalid relay token\n"


@pytest.mark.parametrize(
    "status",
    [
        ImpersonationStatus.REQUESTED,
        ImpersonationStatus.ACCEPTED,
        ImpersonationStatus.ACTIVE,
        ImpersonationStatus.RELEASED,
        ImpersonationStatus.EXPIRED,
    ],
)
def test_wire_lease_status_restores_shared_enum(
    status: ImpersonationStatus,
    lease_at: LeaseAt,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    pending_consent = (ImpersonationStatus.REQUESTED, ImpersonationStatus.ACCEPTED)
    lease, _, mid = lease_at(status if status in pending_consent else ImpersonationStatus.ACTIVE)
    name = machine_identity.name()
    if status is ImpersonationStatus.RELEASED:
        leases.release(database, event_bus, lease["id"], "Done", local_machine=name)
    elif status is ImpersonationStatus.EXPIRED:
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
    snapshot = read_relay_inbox(
        database,
        event_bus,
        lease["agent_id"],
        UUID(lease["id"]),
        lease["relay_token"],
        local_machine=name,
    )
    assert snapshot.status is status
    assert snapshot.active is (status is ImpersonationStatus.ACTIVE)
    if status is ImpersonationStatus.ACTIVE:
        assert [m.id for m in snapshot.messages] == [mid]
        assert snapshot.messages[0].content == "Unprocessed user input"
        assert snapshot.start_message == START_MESSAGE
        assert (snapshot.budget.ack_window_seconds, snapshot.budget.max_delivery_attempts) == (
            180,
            2,
        )
    else:
        assert snapshot.message_ids == frozenset()


def test_a_rolled_back_acceptance_is_read_as_rejected_with_its_reason(
    lease_at: LeaseAt,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, owner, _ = lease_at(ImpersonationStatus.ACCEPTED)
    lease_relay.fail_acceptance(database, event_bus, lease["id"], owner, "relay would not start")
    snapshot = read_relay_inbox(
        database,
        event_bus,
        lease["agent_id"],
        UUID(lease["id"]),
        lease["relay_token"],
        local_machine=machine_identity.name(),
    )
    assert snapshot.status is ImpersonationStatus.REJECTED
    assert snapshot.end_reason == "relay would not start"
    assert snapshot.ended is not None and snapshot.ended.status == "rejected"


def test_write_heartbeat_stops_at_a_terminal_lease(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    name = machine_identity.name()
    token, lease_id = lease["relay_token"], UUID(lease["id"])
    assert write_heartbeat(database, lease_id, token, local_machine=name) is True
    beat = db_conn.execute(
        "SELECT relay_heartbeat_at IS NOT NULL FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone()
    assert beat == (True,)
    leases.release(database, event_bus, lease["id"], "Done", local_machine=name)
    assert write_heartbeat(database, lease_id, token, local_machine=name) is False
