# meiva:note
# The authentication files a Postgres start writes follow the plan for the real OS user.
#
# The always-authenticated pg_hba and the monitor's pg_ident map are the pure functions of
# `pg_plan`; this is the shell writing them: the files on disk are exactly what the plan renders
# for this OS user, the monitoring identity of `base.cluster.authority` and the config's trusted
# CIDRs, and the reach a declared address adds.
#
# The server's own reading of them (the loaded hba, the refused password-less dial) is in
# `test_cluster_instance_native.py`.

from __future__ import annotations

import dataclasses
import getpass

from base.clock import SYSTEM_TIMERS
from base.cluster.authority.monitor import MONITOR_MAP, MONITOR_ROLE
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.cluster_instance import StartTuning
from cli.commands.data_plane.instance.pg_plan import PgIdentity, pg_hba_body, pg_ident_body
from cli.commands.data_plane.tests.native_cluster import NATIVE_TEST_SYSTEM, NativeCluster
from cli.commands.data_plane.tests.native_cluster import native_cluster as native_cluster
from cli.commands.data_plane.tests.native_cluster import retained_children as retained_children
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

_IDENTITY = PgIdentity(
    os_user=getpass.getuser(), monitor_role=MONITOR_ROLE, monitor_map=MONITOR_MAP
)


def _start(cluster: NativeCluster, machine: MachineIdentityReader) -> int:
    return instance.start_pg(
        cluster.plane,
        cluster.pg_port,
        home=cluster.home,
        retained_children=cluster.retained_children,
        machine=machine,
        tuning=StartTuning(bind_wait_s=0.0),
        timers=SYSTEM_TIMERS,
        system=NATIVE_TEST_SYSTEM,
    )


def test_a_single_box_start_writes_the_loopback_only_hba_and_the_monitor_map(
    native_cluster: NativeCluster, command_machine: ConfiguredMachineIdentity
) -> None:
    command_machine.set(host="localhost")

    assert _start(native_cluster, command_machine) == 0

    hba = (native_cluster.pg_data / "pg_hba.conf").read_text()
    assert hba == pg_hba_body(_IDENTITY, reachable_host=None, trusted_cidrs="")
    assert "trust" not in hba
    ident = (native_cluster.pg_data / "pg_ident.conf").read_text()
    assert ident == pg_ident_body(_IDENTITY)
    assert ident == f"meiva_monitor {getpass.getuser()} meiva_monitor\n"


def test_trusted_cidrs_reach_the_hba_the_start_writes(
    native_cluster: NativeCluster, command_machine: ConfiguredMachineIdentity
) -> None:
    command_machine.set(host="localhost")
    plane = dataclasses.replace(native_cluster.plane, trusted_cidrs="10.0.0.0/8")
    cluster = dataclasses.replace(native_cluster, plane=plane)

    assert _start(cluster, command_machine) == 0

    hba = (native_cluster.pg_data / "pg_hba.conf").read_text()
    assert hba.splitlines()[-1] == "host all all 10.0.0.0/8 scram-sha-256"
