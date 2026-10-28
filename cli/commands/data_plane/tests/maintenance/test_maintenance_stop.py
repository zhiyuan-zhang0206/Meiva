# meiva:note
# The maintenance stop's shell against real processes.
#
# What the stop decides is asserted as values in `maintenance/test_process_scan.py` and
# `maintenance/test_stop_plan.py`. Here the process table is the real one: a Redis, a pooler
# and an unreadable process of this home or of a sibling home, a Redis that accepts and never
# answers, a Redis connection ended behind the stop's back, and a whole data plane (Postgres,
# Redis and pooler) stopped in order.

from __future__ import annotations

import asyncio
import dataclasses
import os
import socket
import subprocess
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import psutil
import pytest
import redis
from redis.asyncio import Redis as AsyncRedis

from base.clock import SYSTEM_TIMERS
from base.cluster import ownership
from base.cluster import postgres as owned_postgres
from base.cluster.machine import MachineIdentityReader
from base.native_process.ownership import OwnedProcess
from base.telemetry import EventPipeline
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import maintenance_stop
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.maintenance.plan import RedisShutdownMode, ShutdownRedis, StopPostgres
from cli.commands.data_plane.maintenance.process_scan import judge, refusal
from cli.commands.data_plane.stop_waits import StopLegs, deadline_after
from cli.commands.data_plane.tests.native_processes import (
    owned_pooler,
    running_pooler,
    running_redis,
    sleeping_child,
)
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _local_plane(
    config: DataPlaneConfig, *, redis_url: str, pg_port: int = 12345
) -> DataPlaneConfig:
    return dataclasses.replace(
        config,
        db_url=f"postgresql://test@127.0.0.1:{pg_port}/test",
        redis_url=redis_url,
        cluster_secret="",
        redis_admin_password="",
    )


# meiva:note
# A home whose path has this home's path as a prefix, as `~/.meiva-preview` has `~/.meiva`'s.
def _sibling(home: Path) -> Path:
    sibling = Path(f"{home}-preview")
    sibling.mkdir()
    return sibling


# meiva:note
# A missing pidfile or a refused port is not evidence of no local process: a Redis of this home
# that nothing captured is found in the process table and refuses the stop, with its pid.
def test_a_redis_of_this_home_that_nothing_recorded_refuses_the_stop(unit_home: Path) -> None:
    with (
        running_redis(instance.redis_data_dir(unit_home)) as ours,
        pytest.raises(RuntimeError, match=rf"unrecorded or replacement.*redis pid {ours.pid}"),
    ):
        maintenance_stop.require_no_unrecorded({}, home=unit_home, uid=os.getuid())


def test_a_recorded_redis_is_accounted_for(unit_home: Path) -> None:
    with running_redis(instance.redis_data_dir(unit_home)) as ours:
        maintenance_stop.require_no_unrecorded(
            {"redis": frozenset({ours.pid})}, home=unit_home, uid=os.getuid()
        )


# meiva:note
# The uid the stop is handed decides whose processes are this machine's own: a process of another
# uid is reported as that, and a Redis of this home that belongs to another user is not the stop's
# to refuse over.
def test_a_process_of_another_uid_is_reported_as_that() -> None:
    with sleeping_child() as child:
        process = psutil.Process(child.pid)
        mine = maintenance_stop.look_at(process, "postgres", uid=os.getuid())
        other = maintenance_stop.look_at(process, "postgres", uid=os.getuid() + 1)

    assert not mine.other_user
    assert other.other_user


def test_a_redis_of_another_uid_is_not_this_homes(unit_home: Path) -> None:
    with running_redis(instance.redis_data_dir(unit_home)):
        maintenance_stop.require_no_unrecorded({}, home=unit_home, uid=os.getuid() + 1)


# meiva:note
# A co-located cluster's processes share the names: a Redis and a pooler of a sibling home,
# whose path has this home's as a prefix, are none of this stop's business.
def test_processes_of_a_sibling_home_are_not_unrecorded(unit_home: Path) -> None:
    sibling = _sibling(unit_home)
    with running_redis(sibling / "redis"), running_pooler(sibling):
        maintenance_stop.require_no_unrecorded({}, home=unit_home, uid=os.getuid())


@pytest.mark.parametrize("relative_ini", [False, True])
def test_a_pooler_of_this_home_that_nothing_recorded_refuses_the_stop(
    unit_home: Path, relative_ini: bool
) -> None:
    with running_pooler(unit_home, relative_ini=relative_ini) as ours:
        with pytest.raises(
            RuntimeError, match=rf"unrecorded or replacement.*pgbouncer pid {ours.pid}"
        ):
            maintenance_stop.require_no_unrecorded({}, home=unit_home, uid=os.getuid())

        maintenance_stop.require_no_unrecorded(
            {"pgbouncer": frozenset({ours.pid})}, home=unit_home, uid=os.getuid()
        )


# meiva:note
# The 2026-10 flake: a same-user process whose working directory cannot be read made the scan
# raise instead of judge. On Linux a process that clears its dumpable flag is exactly that for
# every other process of its user (as a hardened or exec-ing process is), and it is read here
# as the scan reads a candidate. It is reported as unreadable, which `refusal` turns into an
# operator-readable refusal naming the pid. macOS cannot make a same-user process unreadable;
# the judgment itself is asserted as values in `maintenance/test_process_scan.py`.
@pytest.mark.requires_host("linux")
def test_a_process_whose_directory_cannot_be_read_is_reported_not_raised() -> None:
    assert os.geteuid() != 0, "root reads every working directory; this needs the suite's user"
    with sleeping_child(undumpable=True) as child:
        seen = maintenance_stop.look_at(psutil.Process(child.pid), "postgres", uid=os.getuid())

        assert seen.locator is None and not seen.other_user and not seen.exiting
        problem = refusal(judge((seen,), homes={"postgres": "/h/pg"}, recorded={}))
        assert problem is not None and f"postgres pid {child.pid}" in problem
        assert "cannot tell whether a data-plane process belongs to this home" in problem


# meiva:note
# A Redis that accepts connections and never answers is the fault a real server does not
# produce on demand and the one that must not hold a stop open: every wait of the stop is bound
# by its one deadline, and the cleanup after it does not wait another socket timeout.
def test_a_redis_that_never_answers_cannot_hold_the_stop_past_its_deadline(
    unit_home: Path,
    redis_access: None,
    data_plane_config: DataPlaneConfig,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        plane = _local_plane(
            data_plane_config, redis_url=f"redis://127.0.0.1:{silent.getsockname()[1]}"
        )
        started = time.monotonic()

        with pytest.raises((TimeoutError, RuntimeError)):
            maintenance_stop.stop(
                plane,
                0.3,
                home=unit_home,
                uid=os.getuid(),
                producer=operator_pipeline,
                timers=SYSTEM_TIMERS,
            )

        assert time.monotonic() - started < 1.5


# meiva:note
# Redis keeps a weak reference to the reconnect callback that custody registers: once the
# connection that was verified as this home's Redis is gone, a command would reconnect to
# whatever answers on the port. The stop must refuse instead, and the server must survive. The
# connection is ended from the server side by another client, after custody was taken.
def test_a_redis_connection_ended_after_capture_cannot_shut_down_another_connection(
    unit_home: Path, redis_access: None, emitted_events: EmittedEvents
) -> None:
    del redis_access
    context = maintenance_stop.StopContext(
        unit_home, os.getuid(), StopLegs(), emitted_events.producer
    )
    with running_redis(instance.redis_data_dir(unit_home)) as server:

        async def scenario() -> None:
            # meiva:note
            # redis-py's async command stubs are partially unknown; the pyright directives
            # below say only that.
            deadline = deadline_after(SYSTEM_TIMERS, 10)
            dedicated = AsyncRedis(
                host="127.0.0.1",
                port=server.port,
                decode_responses=True,
                single_connection_client=True,
            )
            custody = ownership.RedisConnectionCustody()
            identity = await custody.capture(
                dedicated,
                port=server.port,
                data_dir=instance.redis_data_dir(unit_home),
                deadline=deadline.at,
            )
            assert identity is not None
            connection_id = await dedicated.client_id()  # pyright: ignore[reportUnknownMemberType]
            async with AsyncRedis(host="127.0.0.1", port=server.port) as other:
                await other.client_kill_filter(_id=str(connection_id))  # pyright: ignore[reportUnknownMemberType]
            try:
                with pytest.raises(RuntimeError, match="connection changed"):
                    await maintenance_stop.request_stop(
                        ShutdownRedis(RedisShutdownMode.SAVE),
                        identity,
                        dedicated,
                        deadline,
                        context,
                    )
            finally:
                await dedicated.aclose()

        asyncio.run(scenario())

        with redis.Redis.from_url(server.url) as probe:  # pyright: ignore[reportUnknownMemberType]
            assert probe.ping(), "the server must survive lost connection custody"  # pyright: ignore[reportUnknownMemberType]


# meiva:note
# A native custody failure is never reported as a stop: the identity handed to the Postgres leg
# is a replacement of the real postmaster (its start identity altered), the leg raises, and
# the real Postgres keeps running.
def test_a_replaced_postmaster_identity_fails_the_leg_and_leaves_postgres_running(
    postgres_access: None,
    redis_access: None,
    unit_home: Path,
    command_machine: MachineIdentityReader,
    data_plane_config: DataPlaneConfig,
    emitted_events: EmittedEvents,
) -> None:
    from base.deploy.tests.containers import free_port

    port = free_port()
    plane = _local_plane(data_plane_config, redis_url="redis://127.0.0.1:1", pg_port=port)
    data = instance.pg_data_dir(unit_home)
    retained: list[subprocess.Popen[bytes]] = []
    context = maintenance_stop.StopContext(
        unit_home, os.getuid(), StopLegs(), emitted_events.producer
    )
    try:
        assert (
            instance.start_pg(
                plane, port, home=unit_home, retained_children=retained, machine=command_machine
            )
            == 0
        )
        real = ownership.postgres(data)
        assert real is not None
        replaced = OwnedProcess(
            real.pid,
            real.birth + 1,
            real.starttime + 1 if real.starttime is not None else None,
        )
        unused_client = AsyncRedis(port=free_port())

        with pytest.raises(RuntimeError, match="replacement"):
            asyncio.run(
                maintenance_stop.request_stop(
                    StopPostgres(),
                    replaced,
                    unused_client,
                    deadline_after(SYSTEM_TIMERS, 10),
                    context,
                )
            )

        assert real.live() and ownership.postgres(data) is not None
    finally:
        owned_postgres.stop(data, timeout=10, retained_children=retained, timers=SYSTEM_TIMERS)


# meiva:note
# The whole local data plane, as a unit with its services already stopped has it: a real
# Postgres, a real Redis and a real pooler of this home. The stop ends the pooler first so its
# clients are disconnected before Postgres goes down, then Postgres, then Redis; says what it
# stopped; and leaves no process of the home, no recorded birth and no listener behind.
def test_the_stop_ends_a_whole_local_data_plane_in_order(
    postgres_access: None,
    redis_access: None,
    unit_home: Path,
    command_machine: MachineIdentityReader,
    data_plane_config: DataPlaneConfig,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from base.deploy.tests.containers import free_port

    pg_port = free_port()
    data = instance.pg_data_dir(unit_home)
    retained: list[subprocess.Popen[bytes]] = []
    try:
        with (
            running_redis(instance.redis_data_dir(unit_home)) as store,
            running_pooler(unit_home) as pool,
        ):
            plane = _local_plane(data_plane_config, redis_url=store.url, pg_port=pg_port)
            assert (
                instance.start_pg(
                    plane,
                    pg_port,
                    home=unit_home,
                    retained_children=retained,
                    machine=command_machine,
                )
                == 0
            )
            postmaster = ownership.postgres(data)
            assert postmaster is not None

            stopped = maintenance_stop.stop(
                plane,
                20,
                home=unit_home,
                uid=os.getuid(),
                retained_children=retained,
                producer=operator_pipeline,
                timers=SYSTEM_TIMERS,
            )

            assert stopped.stopped == ("pgbouncer", "postgres", "redis")
            assert not pool.identity.live() and not postmaster.live()
            assert (
                not psutil.pid_exists(store.pid)
                or not OwnedProcess.capture(psutil.Process(store.pid)).live()
            )
            assert owned_pooler(unit_home) is None and ownership.postgres(data) is None
            with (
                pytest.raises(ConnectionRefusedError),
                closing(socket.create_connection(("127.0.0.1", store.port), timeout=2)),
            ):
                pass
            maintenance_stop.require_no_unrecorded({}, home=unit_home, uid=os.getuid())
    finally:
        owned_postgres.stop(data, timeout=10, retained_children=retained, timers=SYSTEM_TIMERS)
