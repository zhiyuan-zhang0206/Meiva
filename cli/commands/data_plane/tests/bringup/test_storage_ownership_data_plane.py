# meiva:note
# Native readiness never authorizes changes to a foreign data-plane listener, and a start resumes
# the postmaster it already owns instead of replacing it.
#
# The listener, the symlinked data directory, the stopped postmaster and the interrupted
# admission are real: a socket this test holds, a symlink on disk, a SIGSTOPped postmaster and a
# start that gives up before the server answered. What the start must leave behind is read back
# from the custody receipt and the postmaster itself.

from __future__ import annotations

import os
import signal
import socket
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from base.clock import SYSTEM_TIMERS
from base.cluster import ownership, port_preflight
from base.cluster import postgres as pg
from base.cluster.dataplane.pg_admin import pg_admin_url
from base.cluster.machine import MachineIdentityReader
from base.native_process.ownership import OwnedProcess
from base.telemetry import EventPipeline
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import maintenance_stop as plane
from cli.commands.data_plane.cluster_instance import PRODUCTION_TUNING, StartTuning
from cli.commands.data_plane.tests.native_cluster import NATIVE_TEST_SYSTEM, NativeCluster
from cli.commands.data_plane.tests.native_cluster import native_cluster as native_cluster
from cli.commands.data_plane.tests.native_cluster import retained_children as retained_children
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _start(
    cluster: NativeCluster,
    machine: MachineIdentityReader,
    *,
    tuning: StartTuning = PRODUCTION_TUNING,
) -> int:
    return instance.start_pg(
        cluster.plane,
        cluster.pg_port,
        home=cluster.home,
        host=cluster.host,
        retained_children=cluster.retained_children,
        machine=machine,
        tuning=tuning,
        timers=SYSTEM_TIMERS,
        system=NATIVE_TEST_SYSTEM,
        environ=os.environ,
    )


def _assert_pg_birth(cluster: NativeCluster) -> tuple[OwnedProcess, bytes]:
    owner = ownership.require_postgres(cluster.pg_data, cluster.pg_port)
    assert owner is not None
    receipt = pg.read_receipt(cluster.pg_data)
    assert receipt is not None
    assert (receipt.state, receipt.boot_id) == ("ready", pg.boot_id(receipt.platform))
    return owner, pg.receipt_path(cluster.pg_data).read_bytes()


def _assert_warm_repeat(
    cluster: NativeCluster, machine: MachineIdentityReader, first: OwnedProcess, persisted: bytes
) -> None:
    assert _start(cluster, machine) == 0
    repeated = ownership.postgres(cluster.pg_data)
    assert repeated is not None and first.same_birth(repeated)
    assert pg.receipt_path(cluster.pg_data).read_bytes() == persisted


def test_a_foreign_listener_on_the_postgres_port_refuses_before_hba_or_initdb(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    with socket.socket() as held:
        held.bind(("127.0.0.1", native_cluster.pg_port))
        held.listen()

        with pytest.raises(RuntimeError, match="no owned data-plane process"):
            _start(native_cluster, command_machine)

    assert not (native_cluster.pg_data / "pg_hba.conf").exists()
    assert not (native_cluster.pg_data / "PG_VERSION").exists()


# meiva:note
# The ordinary producer supplies all custody; no test-only receipt adoption.
def test_real_owned_postgres_resume_and_fast_stop(
    native_cluster: NativeCluster,
    command_machine: MachineIdentityReader,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    home, port = native_cluster.home, native_cluster.pg_port
    assert _start(native_cluster, command_machine) == 0
    first, persisted = _assert_pg_birth(native_cluster)
    with psycopg.connect(pg_admin_url(port, home=home), autocommit=True) as conn:
        ownership.require_postgres_connection(
            conn, native_cluster.pg_data, instance.pg_socket_dir(home)
        )
        row = conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()
        assert row is not None
        system_id = row[0]
        _assert_warm_repeat(native_cluster, command_machine, first, persisted)
        assert plane.stop(
            native_cluster.plane,
            10,
            home=home,
            uid=os.getuid(),
            retained_children=native_cluster.retained_children,
            producer=operator_pipeline,
            timers=SYSTEM_TIMERS,
        ).stopped == ("postgres",)
        assert not first.live() and ownership.postgres(native_cluster.pg_data) is None
        assert pg.receipt_path(native_cluster.pg_data).read_bytes() == persisted
        with pytest.raises(psycopg.OperationalError):
            conn.execute("SELECT 1")
    stopped = plane.stop(
        native_cluster.plane,
        2,
        home=home,
        uid=os.getuid(),
        retained_children=native_cluster.retained_children,
        producer=operator_pipeline,
        timers=SYSTEM_TIMERS,
    )
    assert stopped.stopped == ()

    assert _start(native_cluster, command_machine) == 0

    second, _receipt = _assert_pg_birth(native_cluster)
    assert not first.same_birth(second)
    with psycopg.connect(pg_admin_url(port, home=home), autocommit=True) as conn:
        ownership.require_postgres_connection(
            conn, native_cluster.pg_data, instance.pg_socket_dir(home)
        )
        identifier = conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()
        assert identifier == (system_id,)
    with pytest.raises(RuntimeError, match="replacement"):
        pg.stop(
            native_cluster.pg_data,
            expected=first,
            retained_children=native_cluster.retained_children,
            timers=SYSTEM_TIMERS,
        )
    assert second.live()


def test_a_pgdata_symlink_cannot_adopt_another_homes_receipt(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign" / "pg"
    foreign.mkdir(parents=True)
    hba = foreign / "pg_hba.conf"
    hba.write_text("foreign authority\n")
    native_cluster.pg_data.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        _start(native_cluster, command_machine)

    assert hba.read_text() == "foreign authority\n"
    assert not (foreign / "PG_VERSION").exists()
    assert not (foreign.parent / "run").exists()


def test_a_stopped_postmaster_cannot_pass_warm_readiness(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    owner, before = _assert_pg_birth(native_cluster)
    assert owner.send_signal(signal.SIGSTOP)
    try:
        with pytest.raises(RuntimeError, match="did not become ready"):
            _start(
                native_cluster, command_machine, tuning=StartTuning(pg_start_s=0.25, pg_probe_s=0.1)
            )
        # meiva:note
        # A retained birth is never replaced: its custody receipt is byte-identical, the same
        # native process is alive and still the home's owner.
        assert owner.live() and pg.receipt_path(native_cluster.pg_data).read_bytes() == before
        still = ownership.postgres(native_cluster.pg_data)
        assert still is not None and owner.same_birth(still)
    finally:
        owner.send_signal(signal.SIGCONT)
    _assert_warm_repeat(native_cluster, command_machine, owner, before)


# meiva:note
# The first start is cut off after the postmaster was launched and captured but before the server
# answered. A later start admits that very process rather than launching another.
def test_a_start_that_gave_up_before_the_server_answered_completes_the_same_postmaster(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    with pytest.raises(RuntimeError, match="did not become ready"):
        _start(
            native_cluster, command_machine, tuning=StartTuning(pg_start_s=0.01, pg_probe_s=0.01)
        )
    captured = pg.read_receipt(native_cluster.pg_data)
    assert captured is not None and captured.state == "captured"
    owner = captured.process()

    assert _start(native_cluster, command_machine) == 0

    admitted, persisted = _assert_pg_birth(native_cluster)
    assert owner.same_birth(admitted)
    _assert_warm_repeat(native_cluster, command_machine, owner, persisted)
    port_preflight.require_listener(admitted, native_cluster.pg_port)
