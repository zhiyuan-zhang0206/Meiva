# meiva:note
# A relay that cannot reach the database: the retry it makes, the fence it still obeys, and the
# exit it takes when the budget is spent. The pool failures are real `PoolTimeout`s from a
# factory that refuses for a while, so the retry runs the production path (lease read, credential
# check) on every attempt instead of a stub's.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from base.agents import impersonation as leases
from base.agents.impersonation import RELAY_CONNECTION_RETRY_DELAYS
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.events.live.bus import EventBus
from cli.commands.agents.impersonation_relay import (
    EXIT_TEMPORARY_FAILURE,
    RelayInputs,
    cmd_relay,
)
from cli.commands.agents.relay.steps import ConnectionRetry, connection_retry_delay
from cli.commands.agents.tests.lease_support import ActiveSession
from cli.commands.agents.tests.lease_support import active as active
from cli.commands.agents.tests.relay_harness import REAL_CLOCK
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database

USER_HOME = Path("/nonexistent-relay-user-home")
# meiva:note
# The production waits on a suite's clock: the delays under test are the real ones (so emptying
# `RELAY_CONNECTION_RETRY_DELAYS` is a regression these tests see), while no test spends the
# budget waiting. The policy is assembled where the invocation hands its timers in, so a relay
# built here never reads a clock of its own.
TIMERS = ManualTimers()


def _relay_args(lease: dict[str, Any]) -> argparse.Namespace:
    return argparse.Namespace(
        lease_id=lease["id"],
        agent_id=lease["agent_id"],
        session_id=None,
        provider="claude",
        thread_id=None,
        codex_remote=None,
        antigravity_endpoint_file=None,
        debounce=0.0,
    )


def _inputs(
    lease: dict[str, Any], *, home: Path, redis_url: str, bus: EventBus, stdout: io.StringIO
) -> RelayInputs:
    return RelayInputs(
        token=lease["relay_token"],
        redis_url=redis_url,
        codex_home=None,
        user_home=USER_HOME,
        home=home,
        antigravity_server=None,
        stdout=stdout,
        bus=bus,
        clock=REAL_CLOCK,
        timers=TIMERS,
    )


class _RefusedPool:
    # meiva:note
    # A database factory that cannot provide a connection `refusals` times and then hands over
    # the real one. `calls` is the assertion surface: a retry that never ran leaves it at one.
    def __init__(self, real: OperatorDatabaseFactory, refusals: int) -> None:
        self.real = real
        self.refusals = refusals
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self.calls <= self.refusals:
            raise PoolTimeout("couldn't get a connection after 30.00 sec")
        return self.real()


def test_a_pool_that_refuses_then_recovers_runs_the_relay_to_its_end(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    redis_url: str,
    event_bus: EventBus,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, _, mid = active
    # meiva:note
    # The lease outlives the outage by enough to push one message: the assertion is that the
    # relay came back at all, not that it waits here.
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '4 seconds' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    pool = _RefusedPool(operator_database, refusals=2)
    stdout = io.StringIO()
    inputs = _inputs(
        lease, home=command_runtime.home, redis_url=redis_url, bus=event_bus, stdout=stdout
    )
    code = cmd_relay(
        _relay_args(lease), inputs=inputs, database_factory=pool, machine=machine_identity
    )
    assert code == 0
    assert pool.calls == 3
    assert f"[id={mid}]" in stdout.getvalue()


def test_a_revoked_credential_ends_the_relay_instead_of_retrying(
    active: ActiveSession,
    redis_url: str,
    event_bus: EventBus,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, owner, _ = active
    # meiva:note
    # The credential is rotated while the pool refuses, so the first real attempt after recovery
    # meets a fence. A relay that retried decisions as well as connections would keep going here.
    leases.provision_relay(operator_database(), lease["id"], owner, "rotated-by-the-host")
    pool = _RefusedPool(operator_database, refusals=1)
    code = cmd_relay(
        _relay_args(lease),
        inputs=_inputs(
            lease,
            home=command_runtime.home,
            redis_url=redis_url,
            bus=event_bus,
            stdout=io.StringIO(),
        ),
        database_factory=pool,
        machine=machine_identity,
    )
    assert code == 1
    assert pool.calls == 2
    assert "Invalid relay token" in capsys.readouterr().err


def test_a_spent_retry_budget_names_the_failure_and_exits_temporary(
    active: ActiveSession,
    redis_url: str,
    event_bus: EventBus,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, _, _ = active
    pool = _RefusedPool(operator_database, refusals=99)
    code = cmd_relay(
        _relay_args(lease),
        inputs=_inputs(
            lease,
            home=command_runtime.home,
            redis_url=redis_url,
            bus=event_bus,
            stdout=io.StringIO(),
        ),
        database_factory=pool,
        machine=machine_identity,
    )
    err = capsys.readouterr().err
    assert code == EXIT_TEMPORARY_FAILURE
    assert pool.calls == len(RELAY_CONNECTION_RETRY_DELAYS) + 1
    assert "could not reach the database while opening the operator database" in err
    assert f"{len(RELAY_CONNECTION_RETRY_DELAYS) + 1} attempt(s)" in err
    assert "PoolTimeout" in err


def test_the_retry_policy_hands_out_each_delay_then_stops() -> None:
    delays = (0.5, 2.0)
    assert connection_retry_delay(1, delays) == 0.5
    assert connection_retry_delay(2, delays) == 2.0
    assert connection_retry_delay(3, delays) is None
    assert connection_retry_delay(0, delays) is None
    assert connection_retry_delay(1, ()) is None
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ConnectionRetry((-1.0,), ManualTimers())
