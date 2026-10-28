# meiva:note
# The cluster's own Postgres, started for real: its settings, its authentication and its custody.
#
# `start_pg` is the shell of `pg_plan`; these tests run it against the host's real `postgres`
# under the test's own home and read back what the running server says it was started with. The
# plans that produced those settings are asserted in `test_pg_plan.py`.

from __future__ import annotations

import getpass
import subprocess

import psutil
import psycopg
import pytest

from base.clock import SYSTEM_TIMERS
from base.cluster import ownership
from base.cluster.dataplane.pg_tools import pg_tool
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.paths import meiva_home
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.cluster_instance import StartTuning
from cli.commands.data_plane.tests.native_cluster import NativeCluster
from cli.commands.data_plane.tests.native_cluster import (
    native_cluster as native_cluster,
)
from cli.commands.data_plane.tests.native_cluster import (
    retained_children as retained_children,
)
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

# meiva:note
# 192.0.2.0/24 (RFC 5737 TEST-NET-1) is never assigned to a real interface, so binding it fails
# with EADDRNOTAVAIL: the exact "private network is not up yet" signal.
_UNASSIGNED = "192.0.2.1"


def _start(
    cluster: NativeCluster, machine: MachineIdentityReader, *, bind_wait_s: float = 0.0
) -> int:
    return instance.start_pg(
        cluster.plane,
        cluster.pg_port,
        home=cluster.home,
        retained_children=cluster.retained_children,
        machine=machine,
        tuning=StartTuning(bind_wait_s=bind_wait_s),
        timers=SYSTEM_TIMERS,
    )


def _setting(cluster: NativeCluster, name: str) -> str:
    socket_dir = instance.pg_socket_dir(cluster.home)
    with psycopg.connect(host=str(socket_dir), port=cluster.pg_port, dbname="postgres") as conn:
        row = conn.execute("SELECT setting FROM pg_settings WHERE name = %s", (name,)).fetchone()
    assert row is not None
    return str(row[0])


def test_the_running_server_carries_the_planned_launch_settings(
    native_cluster: NativeCluster, command_machine: ConfiguredMachineIdentity
) -> None:
    command_machine.set(host="localhost")
    assert _start(native_cluster, command_machine) == 0
    assert _setting(native_cluster, "listen_addresses") == "127.0.0.1"
    assert _setting(native_cluster, "unix_socket_permissions") == "0700"
    assert _setting(native_cluster, "max_connections") == "500"
    assert _setting(native_cluster, "TimeZone") == "UTC"
    assert _setting(native_cluster, "archive_mode") == "off"
    assert _setting(native_cluster, "unix_socket_directories") == str(
        instance.pg_socket_dir(native_cluster.home)
    )
    for name in ("shared_memory_type", "dynamic_shared_memory_type"):
        assert _setting(native_cluster, name) == "mmap"


def test_the_socket_directory_is_owner_only(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    socket_dir = instance.pg_socket_dir(native_cluster.home)
    assert socket_dir.stat().st_mode & 0o777 == 0o700


# meiva:note
# Secret-set cluster, reachable address never assigned: postgres must not be launched into a
# guaranteed bind failure; authentication is already written.
def test_a_declared_reach_that_never_appears_fails_fast_before_launching(
    native_cluster: NativeCluster,
    command_machine: ConfiguredMachineIdentity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command_machine.set(host=_UNASSIGNED)

    assert _start(native_cluster, command_machine, bind_wait_s=0.0) == 1

    err = capsys.readouterr().err
    assert "not assigned to any local interface" in err and "private network" in err
    assert ownership.postgres(native_cluster.pg_data) is None
    assert not (native_cluster.pg_data / "postmaster.pid").exists()
    assert (
        (native_cluster.pg_data / "pg_hba.conf")
        .read_text()
        .startswith(f"local all {getpass.getuser()} peer")
    )


# meiva:note
# A box whose reachable address is loopback binds loopback only and starts at once.
def test_a_single_box_start_probes_no_address(
    native_cluster: NativeCluster, command_machine: ConfiguredMachineIdentity
) -> None:
    command_machine.set(host="localhost")
    assert _start(native_cluster, command_machine, bind_wait_s=0.0) == 0


def test_a_second_start_re_affirms_the_same_postmaster(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    first = ownership.postgres(native_cluster.pg_data)
    assert first is not None

    assert _start(native_cluster, command_machine) == 0

    second = ownership.postgres(native_cluster.pg_data)
    assert second is not None and first.same_birth(second)


# meiva:note
# The postmaster is the longest-lived daemon (retained across releases) and the `meiva start` that
# spawns it holds this process's environment: the test home is in it (`MEIVA_HOME`), and none of it
# crosses to the postmaster.
def test_the_postmaster_inherits_no_meiva_authority(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    owner = ownership.postgres(native_cluster.pg_data)
    assert owner is not None
    inherited = psutil.Process(owner.pid).environ()
    assert not [name for name in inherited if name.startswith("MEIVA_")]
    assert "PATH" in inherited


def test_a_passwordless_dial_over_tcp_is_asked_for_a_password(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    for user in (getpass.getuser(), native_cluster.identity):
        with pytest.raises(psycopg.OperationalError, match="password"):
            psycopg.connect(
                host="127.0.0.1",
                port=native_cluster.pg_port,
                user=user,
                password="",
                dbname="postgres",
                connect_timeout=5,
            )


def test_pg_isready_answers_for_the_running_server_and_not_after_it_stops(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    isready = str(pg_tool("pg_isready", home=meiva_home()))
    assert not instance.pg_running(isready, native_cluster.pg_port, "127.0.0.1")
    assert _start(native_cluster, command_machine) == 0
    assert instance.pg_running(isready, native_cluster.pg_port, "127.0.0.1")
    subprocess.run(
        [
            str(pg_tool("pg_ctl", home=meiva_home())),
            "-D",
            str(native_cluster.pg_data),
            "-m",
            "immediate",
            "stop",
        ],
        check=True,
        capture_output=True,
    )
    for child in native_cluster.retained_children:
        child.wait(timeout=5)
    assert not instance.pg_running(isready, native_cluster.pg_port, "127.0.0.1")
