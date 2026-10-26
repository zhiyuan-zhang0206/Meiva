# meiva:note
# Stopping this cluster's own data plane, against a real Postgres and Redis.
#
# `stop_cluster_instance` is the shell of `stop_plan`: pooler, Postgres under native custody, then
# Redis as its admin user (the runtime URL's ACL user can neither AUTH as `default` nor SHUTDOWN).
# Redis counts as stopped only when its port stops answering, whatever `shutdown` exited with. A
# remote-managed plane has nothing to stop but may have an old local instance to warn about.

from __future__ import annotations

import contextlib
import dataclasses
import os
import socket
import threading
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from base.cluster import ownership
from base.cluster.dataplane.pg_tools import pg_tool
from base.cluster.machine import MachineIdentityReader
from base.telemetry import EventPipeline
from cli.commands.data_plane import bringup, cluster_instance
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import (
    remote_data_plane_config as remote_data_plane_config,
)
from cli.commands.data_plane.tests.data_plane_records import with_redis_admin
from cli.commands.data_plane.tests.native_cluster import (
    ADMIN_PASSWORD,
    NATIVE_TEST_MACOS,
    RUNTIME_PASSWORD,
    NativeCluster,
)
from cli.commands.data_plane.tests.native_cluster import (
    native_cluster as native_cluster,
)
from cli.commands.data_plane.tests.native_cluster import (
    retained_children as retained_children,
)
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _start(cluster: NativeCluster, machine: MachineIdentityReader) -> None:
    rc = cluster_instance.ensure_cluster_storage(
        cluster.plane,
        home=cluster.home,
        pg_port=cluster.pg_port,
        redis_port=cluster.redis_port,
        redis_password=RUNTIME_PASSWORD,
        redis_user=cluster.identity,
        retained_children=cluster.retained_children,
        machine=machine,
        macos=NATIVE_TEST_MACOS,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
    )
    assert rc == 0


def _stop(cluster: NativeCluster, producer: Callable[[], EventPipeline]) -> int:
    return bringup.stop_cluster_instance(
        cluster.plane,
        home=cluster.home,
        retained_children=cluster.retained_children,
        producer=producer,
        macos=NATIVE_TEST_MACOS,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
    )


def _answers(cluster: NativeCluster) -> tuple[bool, bool]:
    postgres = cluster_instance.pg_running(str(pg_tool("pg_isready")), cluster.pg_port, "127.0.0.1")
    redis = cluster_instance.redis_reachable(ADMIN_PASSWORD, cluster.redis_port, "127.0.0.1")
    return postgres, redis


def test_stop_takes_both_services_down_and_closes_postgres_custody(
    native_cluster: NativeCluster,
    command_machine: MachineIdentityReader,
    operator_pipeline: Callable[[], EventPipeline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _start(native_cluster, command_machine)
    assert _answers(native_cluster) == (True, True)
    capsys.readouterr()

    assert _stop(native_cluster, operator_pipeline) == 0

    assert _answers(native_cluster) == (False, False)
    assert ownership.postgres(native_cluster.pg_data) is None
    out = capsys.readouterr().out
    assert "stopping per-cluster data plane" in out
    assert "✓ postgres stopped" in out and "✓ redis stopped" in out
    assert out.index("postgres stopped") < out.index("redis stopped")


def test_the_data_survives_a_stop_on_disk(
    native_cluster: NativeCluster,
    command_machine: MachineIdentityReader,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    _start(native_cluster, command_machine)
    assert _stop(native_cluster, operator_pipeline) == 0
    assert (native_cluster.pg_data / "PG_VERSION").is_file()
    assert (native_cluster.home / "redis" / "redis.conf").is_file()
    _start(native_cluster, command_machine)
    assert _answers(native_cluster) == (True, True)


def test_stopping_a_plane_that_is_not_running_is_a_clean_no_op(
    native_cluster: NativeCluster,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    assert _answers(native_cluster) == (False, False)
    assert _stop(native_cluster, operator_pipeline) == 0


# meiva:note
# The exit status of `shutdown` proves nothing; Redis that still answers fails the stop.
def test_a_redis_that_the_admin_cannot_shut_down_is_a_failed_stop(
    native_cluster: NativeCluster,
    command_machine: MachineIdentityReader,
    operator_pipeline: Callable[[], EventPipeline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _start(native_cluster, command_machine)
    wrong_admin = with_redis_admin(native_cluster.plane, "not-the-admin")
    capsys.readouterr()

    rc = bringup.stop_cluster_instance(
        wrong_admin,
        home=native_cluster.home,
        retained_children=native_cluster.retained_children,
        producer=operator_pipeline,
        macos=NATIVE_TEST_MACOS,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert f"redis still answers on 127.0.0.1:{native_cluster.redis_port} after shutdown" in err
    assert cluster_instance.redis_reachable(ADMIN_PASSWORD, native_cluster.redis_port, "127.0.0.1")


# meiva:note
# A real listener that accepts and drops every connection, on the port a cluster's Redis owns:
# `redis-cli` reaches it and gets nothing back, the way it would reach a stranger.
@contextlib.contextmanager
def _stranger_listening(port: int) -> Generator[None]:
    server = socket.create_server(("127.0.0.1", port))
    server.settimeout(0.1)
    done = threading.Event()

    def drop_connections() -> None:
        while not done.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            connection.close()

    thread = threading.Thread(target=drop_connections)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()
        server.close()


# meiva:note
# The port decides whether the stop failed; who holds it decides what the failure says. Another
# process on the cluster's Redis port fails the stop, and names itself as another process instead
# of passing for this home's Redis.
def test_a_stranger_on_the_redis_port_fails_the_stop_as_another_process(
    native_cluster: NativeCluster,
    operator_pipeline: Callable[[], EventPipeline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _stranger_listening(native_cluster.redis_port):
        rc = _stop(native_cluster, operator_pipeline)

    assert rc == 1
    err = capsys.readouterr().err
    assert (
        f"127.0.0.1:{native_cluster.redis_port} still accepts connections after shutdown, "
        "held by a process other than this home's redis"
    ) in err
    assert "redis still answers" not in err


# meiva:note
# ─── a remote-managed plane: nothing to stop, but an old local instance gets a hint ──────────


def test_a_remote_plane_stops_nothing_and_says_so(
    remote_data_plane_config: DataPlaneConfig,
    operator_pipeline: Callable[[], EventPipeline],
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = bringup.stop_cluster_instance(
        remote_data_plane_config,
        home=unit_home,
        producer=operator_pipeline,
        macos=NATIVE_TEST_MACOS,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "remote-managed" in out and "nothing to stop locally" in out


# meiva:note
# A cluster that switched local to remote may still have its old local instance running; `meiva
# stop` no longer manages it, so it prints a manual-teardown hint instead of silently leaving it (QA
# P2).
def test_a_local_instance_left_behind_by_a_switch_to_remote_is_named_and_not_touched(
    native_cluster: NativeCluster,
    command_machine: MachineIdentityReader,
    operator_pipeline: Callable[[], EventPipeline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    native_cluster.born()
    _start(native_cluster, command_machine)
    # meiva:note
    # The remote-managed home carries no local Redis admin password: the leftover Redis is
    # detected by its listener, never by an authenticated PING.
    switched = dataclasses.replace(
        native_cluster.plane,
        db_url=f"postgresql://meiva:pw@10.9.8.7:{native_cluster.pg_port}/meiva",
        redis_admin_password="",
    )
    capsys.readouterr()

    rc = bringup.stop_cluster_instance(
        switched,
        home=native_cluster.home,
        producer=operator_pipeline,
        macos=NATIVE_TEST_MACOS,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
    )

    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "local postgres + redis from before the switch is still running" in combined
    assert "no longer managed" in combined
    assert _answers(native_cluster) == (True, True)
