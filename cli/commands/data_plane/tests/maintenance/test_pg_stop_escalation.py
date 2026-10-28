# meiva:note
# A Postgres fast shutdown blocked by a hung archive command is ended, not waited on forever.
#
# The real Postgres runs with an archive command that never returns (`sleep`, a stand-in for a
# wal-g stuck on a blackholed network). A fast shutdown cannot finish while the archiver waits
# on it; the data-plane stop must end it with an immediate shutdown inside its budget, kill the
# archive command it left behind, and say so, without failing the stop. The arithmetic of the
# budget is asserted as values in `maintenance/test_stop_plan.py` and `test_stop_budget.py`.

from __future__ import annotations

import dataclasses
import getpass
import os
import subprocess
import time
from pathlib import Path

import psutil
import psycopg
import pytest

from base.clock import SYSTEM_TIMERS, Deadline
from base.cluster import ownership
from base.cluster import postgres as pg
from base.cluster.dataplane.pg_admin import pg_admin_url
from base.cluster.machine import MachineIdentityReader
from base.native_process.ownership import capture_tree
from base.paths import meiva_home
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import maintenance_stop as plane
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.stop_waits import StopLegs, remaining
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

# meiva:note
# A marker only this test's archive command carries.
HUNG_SECONDS = "3617"


def _private_plane(config: DataPlaneConfig) -> tuple[int, DataPlaneConfig]:
    from base.deploy.tests.containers import free_port

    port = free_port()
    return port, dataclasses.replace(
        config,
        db_url=f"postgresql://test@127.0.0.1:{port}/test",
        redis_url=f"redis://127.0.0.1:{free_port()}",
        cluster_secret="",
        redis_admin_password="",
    )


def _hung_archive_commands(owner: pg.OwnedProcess) -> list[psutil.Process]:
    found: list[psutil.Process] = []
    for member in capture_tree(owner):
        process = psutil.Process(member.pid)
        if HUNG_SECONDS in " ".join(process.cmdline()) and process.name() == "sleep":
            found.append(process)
    return found


def _wait_for_hung_archive_command(data: Path, port: int) -> list[psutil.Process]:
    owner = ownership.postgres(data)
    assert owner is not None
    deadline = time.monotonic() + 30
    with psycopg.connect(pg_admin_url(port, home=meiva_home()), autocommit=True) as conn:
        while time.monotonic() < deadline:
            conn.execute("SELECT pg_logical_emit_message(true, 'x', 'y')")
            conn.execute("SELECT pg_switch_wal()")
            if found := _hung_archive_commands(owner):
                return found
            time.sleep(0.2)
    raise AssertionError("the archiver never started the archive command")


# meiva:note
# Start Postgres with a hung archive command: the cluster's data directory is made the way the
# ordinary start makes it, and `postgresql.auto.conf` (what `ALTER SYSTEM` writes, and the
# postmaster reads at launch) carries the archive settings before the one `start_pg`.
def _start_with_hung_archive(
    private: DataPlaneConfig,
    port: int,
    home: Path,
    retained: list[subprocess.Popen[bytes]],
    machine: MachineIdentityReader,
) -> None:
    data = instance.ensure_pg_data(home, os_user=getpass.getuser())
    with (data / "postgresql.auto.conf").open("a") as settings:
        settings.write("archive_mode = 'on'\n")
        settings.write(f"archive_command = 'sleep {HUNG_SECONDS}'\n")
        settings.write("archive_timeout = '1s'\n")
    assert (
        instance.start_pg(
            private,
            port,
            home=home,
            retained_children=retained,
            machine=machine,
            timers=SYSTEM_TIMERS,
        )
        == 0
    )


def test_a_hung_archive_command_ends_in_an_immediate_shutdown_inside_the_budget(
    postgres_access: None,
    unit_home: Path,
    emitted_events: EmittedEvents,
    *,
    command_machine: MachineIdentityReader,
    data_plane_config: DataPlaneConfig,
) -> None:
    port, private = _private_plane(data_plane_config)
    data = instance.pg_data_dir(unit_home)
    retained: list[subprocess.Popen[bytes]] = []
    try:
        _start_with_hung_archive(private, port, unit_home, retained, command_machine)
        owner = ownership.postgres(data)
        assert owner is not None
        hung_pids = {process.pid for process in _wait_for_hung_archive_command(data, port)}

        # meiva:note
        # Small legs: the reserve is cleanup + kill + cleanup = 3 s, so a 7 s stop gives the
        # fast shutdown 4 s before it is ended.
        started = time.monotonic()
        stopped = plane.stop(
            private,
            7,
            home=unit_home,
            uid=os.getuid(),
            retained_children=retained,
            producer=emitted_events.producer,
            timers=SYSTEM_TIMERS,
            legs=StopLegs(cleanup_wait_s=1.0, kill_wait_s=1.0),
        )
        elapsed = time.monotonic() - started

        assert stopped.stopped == ("postgres",)
        assert elapsed < 7, "the stop completed inside its budget"
        assert not owner.live() and ownership.postgres(data) is None
        assert not [pid for pid in hung_pids if psutil.pid_exists(pid)], (
            "the archive command is gone"
        )
        (note,) = stopped.notes
        assert "fast shutdown did not complete" in note and "immediate shutdown" in note
        (event,) = emitted_events.named("postgres_stop_escalated")
        assert event.level == "error" and event.source == "stop"
        assert set(event.attributes["killed"]) <= hung_pids | {owner.pid}
    finally:
        pg.stop(
            data,
            timeout=10,
            immediate_wait=1,
            kill_wait=1,
            retained_children=retained,
            timers=SYSTEM_TIMERS,
        )
        for process in psutil.process_iter(["cmdline"]):
            if HUNG_SECONDS in " ".join(process.info["cmdline"] or []):
                process.kill()


# meiva:note
# A stop whose deadline is already spent is refused before the fast shutdown is asked for any
# time; it neither starts a shutdown nor relies on a busy host to spend the budget.
@pytest.mark.parametrize("offset", [0.0, -1.0])
def test_an_exhausted_stop_budget_refuses_admission(offset: float) -> None:
    with pytest.raises(TimeoutError, match="stop deadline expired"):
        remaining(Deadline(at=time.monotonic() + offset, timers=SYSTEM_TIMERS))
