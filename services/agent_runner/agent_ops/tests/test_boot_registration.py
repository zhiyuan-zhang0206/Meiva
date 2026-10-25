# meiva:note
# The ops daemon announces its own unit once it serves, and where it binds.

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from base.cluster import machines
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from services.agent_runner.agent_ops import boot


@contextmanager
def _machines_table_missing(conn: psycopg.Connection) -> Generator[None]:
    conn.commit()
    conn.execute("ALTER TABLE machines RENAME TO machines_hidden_by_test")
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("ALTER TABLE machines_hidden_by_test RENAME TO machines")
        conn.commit()


# meiva:note
# /ops always authenticates; only a host that declares a reachable address
# (`MEIVA_MACHINE_HOST`) binds 0.0.0.0, a single box binds loopback. Which tokens /ops
# accepts is the write-generation matrix in
# tests/components/lifecycle/db_authority/test_api_tokens.py.
def test_ops_binds_all_interfaces_only_for_a_declared_reachable_address(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(host="10.0.0.5")
    # meiva:note
    # See the directive.
    assert boot.ops_bind_host(machine=machine_identity) == "0.0.0.0"  # noqa: S104
    machine_identity.set(host="localhost")
    assert boot.ops_bind_host(machine=machine_identity) == "127.0.0.1"


# meiva:note
# The daemon registers its OWN unit once it is serving — the same
# `register_self` write `meiva start` makes. The URL comes from the shared `unit_dial_url()`
# with this unit's capability set, so the daemon cannot advertise a different address than
# `meiva start` did.
def test_register_boot_announces_this_unit_up_at_its_dial_url(
    db_conn: psycopg.Connection,
    machine_identity: ConfiguredMachineIdentity,
    ops_database: Callable[[], Database],
) -> None:
    machine_identity.set(name="runner-a", role="agent-runner", host="10.0.0.2", description=None)

    boot.register_boot(
        database=ops_database,
        config=snapshot_process_config(),
        machine=machine_identity,
        ops_port=8600,
        home=Path("~/.meiva"),
    )

    assert machines.list_agent_runners(ops_database()) == [("runner-a", "http://10.0.0.2:8600")]


# meiva:note
# A failed registration refresh leaves dispatch entirely correct, so it is
# logged and swallowed. Exiting here would hand the watchdog a respawn loop and
# take the host dark for the gateway — the outage this call exists to prevent.
def test_register_boot_failure_does_not_stop_the_daemon(
    db_conn: psycopg.Connection,
    machine_identity: ConfiguredMachineIdentity,
    caplog: pytest.LogCaptureFixture,
    ops_database: Callable[[], Database],
) -> None:
    machine_identity.set(name="runner-a", role="agent-runner", host="10.0.0.2")

    with _machines_table_missing(db_conn):
        boot.register_boot(
            database=ops_database,
            config=snapshot_process_config(),
            machine=machine_identity,
            ops_port=8600,
            home=Path("~/.meiva"),
        )

    assert "boot registration failed" in caplog.text
    assert "machines" in caplog.text


# meiva:note
# The bug this call fixes, end to end against the real tables.
#
# A host that announced `meiva stop` carries a `stopped_at` latch that only a
# `register_self` clears. When it comes back some other way — an OS-scheduled
# autostart, a watchdog respawn, a rollout's restart leg — nothing used to clear
# it, so the roster reported the host stopped while its ops daemon served every
# op, and the update fan-out (which filters on that same latch) dropped it.
# Registering at the daemon's own boot reconciles both.
def test_register_boot_unstops_a_host_that_came_back(
    db_conn: psycopg.Connection,
    machine_identity: ConfiguredMachineIdentity,
    ops_database: Callable[[], Database],
) -> None:
    config = snapshot_process_config()
    machine_identity.set(name="came-back", role="agent-runner", host="10.0.0.9")
    db_conn.execute("TRUNCATE machines")
    db_conn.execute("TRUNCATE machine_units")
    db_conn.commit()

    machines.register_self(
        ops_database(),
        url="http://10.0.0.9:8600",
        name=machine_identity.name(),
        roles=machine_identity.role(),
        description=machine_identity.description(),
        home="~/.meiva",
        gateway=config.view.gateway.gateway_url.strip() or None,
    )
    machines.mark_stopping(ops_database(), "came-back", "~/.meiva")
    # meiva:note
    # dropped from the fan-out
    assert machines.list_agent_runners(ops_database()) == []
    assert machines.list_stopped_agent_runners(ops_database()) == [
        ("came-back", "http://10.0.0.9:8600")
    ]

    boot.register_boot(
        database=ops_database,
        config=config,
        machine=machine_identity,
        ops_port=8600,
        home=Path("~/.meiva"),
    )

    assert machines.list_agent_runners(ops_database()) == [("came-back", "http://10.0.0.9:8600")]
    assert machines.list_stopped_agent_runners(ops_database()) == []
