# meiva:note
# Strict local maintenance stop uses real private processes, never force fallback.
#
# The data-plane stop takes the plane's configuration as a value, and what it signals is a real
# Redis and a real PgBouncer launched under the test's home.

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import psutil
import pytest

from base.cluster import ownership
from base.cluster.dataplane import pooler as base_pooler
from base.cluster.machine import MachineIdentity
from base.native_process import pid_starttime_ticks
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import client, closure
from base.sessions.pty.paths import ledger_path
from base.sessions.pty.tests.pty_shells import new as new_session
from base.telemetry import EventPipeline
from cli.commands.data_plane import maintenance_stop as plane
from cli.commands.data_plane import pgbouncer as pb
from cli.commands.data_plane.config import DataPlaneConfig, data_plane_config_from_boot
from cli.commands.data_plane.stop_waits import deadline_after, wait_for_exit
from cli.commands.lifecycle.stop import shell_sessions as stop
from cli.commands.lifecycle.tests.stop.stop_support import Launcher, PtyServiceProcess
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop.stop_support import pty_service as pty_service
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.agent_runner.pty_sessions import ledger

CHILD = Path(__file__).with_name("_child.py")


@pytest.fixture
def local_plane(command_runtime: CommandRuntime) -> DataPlaneConfig:
    """A data plane on this machine whose endpoints nothing answers on yet."""
    return replace(
        data_plane_config_from_boot(command_runtime.config),
        db_url="postgresql://test@127.0.0.1:12345/test",
        redis_url="redis://127.0.0.1:12346",
        cluster_secret="",
        redis_admin_password="",
    )


# meiva:note
# The data-plane stop of one unit, as the stop's data-plane phase runs it.
def stop_plane(
    config: DataPlaneConfig, timeout: float, producer: Callable[[], EventPipeline]
) -> list[str]:
    return plane.stop(config, timeout, producer=producer)


# meiva:note
# A shell session refuses a stop that closes none; nothing is signalled.
def test_a_session_in_the_service_refuses_before_any_signal(
    home: Path, launch: Launcher, pty_service: PtyServiceProcess
) -> None:
    del pty_service
    proc = launch("meiva-agent-host")
    assert new_session("meiva-agent-123-shell-1", home)
    with pytest.raises(RuntimeError, match="will not kill or replay"):
        stop.require_no_shell_sessions()
    assert proc.poll() is None


def test_no_shell_session_is_no_refusal(home: Path, pty_service: PtyServiceProcess) -> None:
    del home, pty_service
    stop.require_no_shell_sessions()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_refuses(
    timeout: float,
    home: Path,
    local_plane: DataPlaneConfig,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        stop_plane(local_plane, timeout, operator_pipeline)


# meiva:note
# `OwnedProcess.live` lives in base.native_process.ownership (the stop path and the frontend
# identity probe share it); the Linux start-tick reader is that module's global, so these two
# tests of its three outcomes patch the reference `live()` consults.
def test_linux_ticks_win_over_changed_epoch_birth(
    launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = launch("unused", term="ignore")
    tick: int | None = 123

    def read_tick(_pid: int) -> int | None:
        return tick

    import base.native_process.ownership

    monkeypatch.setattr(base.native_process.ownership, "pid_starttime_ticks", read_tick)
    identity = OwnedProcess(proc.pid, 0, 123)
    assert identity.live()
    tick = 124
    assert not identity.live()
    tick = None
    with pytest.raises(RuntimeError, match="cannot verify"):
        identity.live()


@pytest.mark.requires_host("linux")
def test_wait_for_exit_converges_when_a_tracked_entry_vanishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked process exiting (reaped) mid-wait converges the wait instead
    of aborting the stop — the 2026-09-20 wave-2 failure: the identity read
    found no /proc entry after psutil had validated the pid."""
    import base.native_process.ownership

    child = subprocess.Popen([sys.executable, str(CHILD), "sleep", "30"])
    pid = child.pid
    starttime = pid_starttime_ticks(pid)
    assert starttime is not None
    identity = OwnedProcess(pid, 0.5, starttime)
    real_read = pid_starttime_ticks

    def reaping_read(reading_pid: int) -> int | None:
        # The reap lands exactly where the kernel race puts it: after psutil
        # validated the pid, before the raw read completes.
        if reading_pid == pid:
            os.kill(pid, signal.SIGKILL)
            child.wait()
        return real_read(reading_pid)

    monkeypatch.setattr(base.native_process.ownership, "pid_starttime_ticks", reaping_read)
    try:
        wait_for_exit({identity}, deadline_after(5))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_a_wait_returns_as_soon_as_the_tracked_process_has_exited() -> None:
    child = subprocess.Popen([sys.executable, str(CHILD), "sleep", "0.4"])
    identity = OwnedProcess.capture(psutil.Process(child.pid))
    try:
        started = time.monotonic()
        wait_for_exit({identity}, deadline_after(10))
        assert 0.2 <= time.monotonic() - started < 5
    finally:
        child.wait(timeout=10)
    assert not identity.live()


def test_a_wait_that_outlives_its_deadline_names_the_survivors() -> None:
    child = subprocess.Popen([sys.executable, str(CHILD), "sleep", "30"])
    identity = OwnedProcess.capture(psutil.Process(child.pid))
    try:
        with pytest.raises(TimeoutError, match=rf"did not exit: \[{child.pid}\]"):
            wait_for_exit({identity}, deadline_after(0.2))
        assert identity.live(), "a wait never signals what it waits for"
    finally:
        child.kill()
        child.wait(timeout=10)


# meiva:note
# Issue #2123: the wait follows a tracked process's descendants, so a leader that exits
# first does not let the stop go on while its children still run.
def test_a_leader_that_exits_without_closing_its_children_does_not_end_the_wait() -> None:
    # meiva:note
    # A leader that lives 0.3s and a child it started that outlives it by a second.
    leader = subprocess.Popen([sys.executable, str(CHILD), "sleep", "0.3", "1.2"])
    identity = OwnedProcess.capture(psutil.Process(leader.pid))
    started = time.monotonic()
    wait_for_exit({identity}, deadline_after(10))
    assert time.monotonic() - started >= 0.9, "the child outlived its leader and held the wait"
    leader.wait(timeout=10)


@dataclass(frozen=True)
class Redis:
    url: str
    port: int
    pid: int
    data: Path


# meiva:note
# A real redis-server whose data directory is `directory`: what the stop must be able to own.
@contextmanager
def running_redis(directory: Path, *, password: str | None = None) -> Generator[Redis]:
    from base.deploy.tests.containers import free_port, wait_port

    assert shutil.which("redis-server") is not None, "the suite's CI image installs redis-server"
    directory.mkdir(parents=True, exist_ok=True)
    port = free_port()
    argv = [
        "redis-server",
        "--port",
        str(port),
        "--bind",
        "127.0.0.1",
        "--save",
        "",
        "--appendonly",
        "no",
        "--dir",
        str(directory),
    ]
    if password is not None:
        argv += ["--requirepass", password]
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(port)
        yield Redis(f"redis://127.0.0.1:{port}", port, process.pid, directory)
    finally:
        # meiva:note
        # SIGKILL: a shell session's SIGTERM=SIG_IGN is inherited, so the graceful call would
        # leave this redis alive.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_remote_plane_refuses_without_any_signal(
    local_plane: DataPlaneConfig, operator_pipeline: Callable[[], EventPipeline]
) -> None:
    remote = replace(local_plane, redis_url="redis://192.0.2.4:6379")
    with pytest.raises(RuntimeError, match="remote-managed"):
        stop_plane(remote, 1, operator_pipeline)


def test_recycled_pooler_pid_is_not_stopped(
    local_plane: DataPlaneConfig, home: Path, operator_pipeline: Callable[[], EventPipeline]
) -> None:
    path = home / "pgbouncer/pgbouncer.pid"
    path.parent.mkdir()
    path.write_text(str(os.getpid()))
    with pytest.raises(RuntimeError, match="PgBouncer"):
        stop_plane(local_plane, 1, operator_pipeline)
    assert path.exists()


def test_real_redis_stops_owned_instance_only(
    redis_access: None,
    local_plane: DataPlaneConfig,
    home: Path,
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    import redis

    with (
        running_redis(ownership.redis_data_dir()) as owned,
        running_redis(tmp_path / "sibling-redis") as sibling,
    ):
        with redis.Redis.from_url(owned.url, decode_responses=True) as owned_client:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            owned_client.set("owned-test", "old")
            owned_client.save()  # pyright: ignore[reportUnknownMemberType] — redis stubs
            owned_client.set("owned-test", "latest-unsaved")
        config = replace(local_plane, redis_url=owned.url)
        assert stop_plane(config, 3, operator_pipeline) == ["redis"]
        assert (
            not OwnedProcess.capture(psutil.Process(owned.pid)).live()
            if psutil.pid_exists(owned.pid)
            else True
        )
        with redis.Redis.from_url(sibling.url) as sibling_client:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            assert sibling_client.ping()  # pyright: ignore[reportUnknownMemberType] — redis stubs
        assert stop_plane(config, 1, operator_pipeline) == []
        # Restart the exact private data directory. This proves SAVE includes
        # the latest in-memory write, not merely that an old RDB existed.
        with running_redis(owned.data) as restarted:
            assert restarted.pid != owned.pid
            with redis.Redis.from_url(restarted.url, decode_responses=True) as restored:  # pyright: ignore[reportUnknownMemberType]
                assert restored.get("owned-test") == "latest-unsaved"  # pyright: ignore[reportUnknownMemberType]
            assert stop_plane(
                replace(local_plane, redis_url=restarted.url), 3, operator_pipeline
            ) == ["redis"]


def test_foreign_redis_directory_refuses_before_local_signals(
    redis_access: None,
    local_plane: DataPlaneConfig,
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    import redis

    with running_redis(tmp_path / "foreign-redis") as foreign:
        config = replace(local_plane, redis_url=foreign.url)
        with pytest.raises(RuntimeError, match="Redis process"):
            stop_plane(config, 2, operator_pipeline)
        with redis.Redis.from_url(foreign.url) as still_there:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            assert still_there.ping()  # pyright: ignore[reportUnknownMemberType] — redis stubs


def _dead_shell(launch: Launcher) -> OwnedProcess:
    proc = launch("dead-shell")
    identity = OwnedProcess.capture(psutil.Process(proc.pid))
    proc.kill()
    proc.wait(timeout=5)
    return identity


# meiva:note
# A recorded job is not a live shell session.
def test_a_dead_service_with_only_a_known_job_does_not_block_shell_session_closure(
    home: Path, launch: Launcher
) -> None:
    journal = home / "run/lifecycle-op.json"
    assert not journal.exists()
    job = launch("surviving-job", term="ignore")
    member = OwnedProcess.capture(psutil.Process(job.pid))
    ledger.write(
        ledger_path(), [closure.Target("meiva-agent-123-shell-1", _dead_shell(launch), (member,))]
    )
    with pytest.raises(client.ServiceDownError):
        client.request("list")
    stop.require_no_shell_sessions()
    stop.await_no_shell_sessions(time.monotonic(), "shell-sessions")
    assert job.poll() is None, "fixture teardown, not shell-session-presence probing, owns this job"
    assert not journal.exists()


def test_a_dead_service_whose_ledger_names_only_dead_processes_does_not_refuse(
    home: Path, launch: Launcher
) -> None:
    del home
    ledger.write(ledger_path(), [closure.Target("meiva-agent-123-shell-1", _dead_shell(launch))])
    stop.require_no_shell_sessions()


def test_redis_admin_credential_is_independent_of_runtime_url(
    redis_access: None,
    local_plane: DataPlaneConfig,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    password = "private-maintenance-test"  # noqa: S105 — private ephemeral test
    with running_redis(ownership.redis_data_dir(), password=password) as owned:
        config = replace(
            local_plane,
            redis_url=owned.url.replace("redis://", "redis://restricted:wrong@"),
            redis_admin_password=password,
        )
        assert stop_plane(config, 3, operator_pipeline) == ["redis"]


@dataclass(frozen=True)
class Pooler:
    port: int
    pid: int
    identity: OwnedProcess
    directory: Path


# meiva:note
# A real PgBouncer whose files are this home's pooler files; killed on exit if still alive.
@contextmanager
def running_pooler(home: Path) -> Generator[Pooler]:
    from base.deploy.tests.containers import free_port, wait_port

    binary = base_pooler.pgbouncer_bin()
    if not (Path(binary).exists() or shutil.which(binary)):
        pytest.skip("native pgbouncer is not installed")
    directory = home / "pgbouncer"
    directory.mkdir()
    port = free_port()
    (directory / "userlist.txt").write_text('"anyone" ""\n')
    ini = directory / "pgbouncer.ini"
    ini.write_text(
        "[databases]\n[pgbouncer]\nlisten_addr=127.0.0.1\n"
        f"listen_port={port}\nauth_type=trust\nauth_file={directory / 'userlist.txt'}\n"
        "admin_users=anyone\nstats_users=anyone\n"
        f"pidfile={directory / 'pgbouncer.pid'}\n"
        f"logfile={directory / 'pgbouncer.log'}\n"
        "unix_socket_dir=\n"
    )
    subprocess.run([binary, "-d", str(ini)], check=True, capture_output=True, timeout=5)
    try:
        wait_port(port, timeout=5)
        pid = _wait_pidfile()
        yield Pooler(port, pid, OwnedProcess.capture(psutil.Process(pid)), directory)
    finally:
        _kill_test_poolers()


def test_real_pgbouncer_normal_exit_and_identity_cleanup(
    local_plane: DataPlaneConfig,
    home: Path,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from base.deploy.tests.containers import free_port

    with running_pooler(home) as pooler:
        config = replace(local_plane, redis_url=f"redis://127.0.0.1:{free_port()}")
        clients: list[str] = []
        assert plane.stop(config, 3, clients=clients, producer=operator_pipeline) == ["pgbouncer"]
        assert not pooler.identity.live()
        assert not (pooler.directory / "pgbouncer.pid").exists()
        # meiva:note
        # This home holds no admin credential, so the console cannot be read: said, never an
        # empty list.
        assert len(clients) == 1 and "could not be listed" in clients[0]


# meiva:note
# The 2026-09-12 incident shape: an idle connected client must not hold the
# pooler stop open.
#
# PgBouncer >=1.23 runs SIGTERM as SHUTDOWN WAIT_FOR_CLIENTS: with any
# connected client (here an idle admin console) it waits for every client to
# disconnect, so a stop under paused runners can only burn its deadline. The
# safe-shutdown SIGINT disconnects clients and waits only for in-flight
# server connections; it exits in well under a second. The clients still connected are
# named once, before the pooler is signalled, and only reported.
def test_real_pgbouncer_stop_does_not_wait_for_idle_client(
    db_url: str,
    local_plane: DataPlaneConfig,
    home: Path,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    import psycopg

    from base.deploy.tests.containers import free_port

    with running_pooler(home) as pooler:
        config = replace(local_plane, redis_url=f"redis://127.0.0.1:{free_port()}")
        clients: list[str] = []
        with psycopg.connect(
            f"postgresql://anyone@127.0.0.1:{pooler.port}/pgbouncer", autocommit=True
        ) as connected:
            # Connected and idle from here on: the stop must not wait for this.
            assert connected.execute("SHOW VERSION").fetchone() is not None
            started = time.monotonic()
            stopped = plane.stop(config, 5, clients=clients, producer=operator_pipeline)
        assert stopped == ["pgbouncer"]
        assert time.monotonic() - started < 4
        assert not pooler.identity.live()
        assert len(clients) == 1, "the console was asked once, before the pooler was signalled"


def test_missing_pooler_pidfile_does_not_mean_the_process_is_gone(
    local_plane: DataPlaneConfig,
    home: Path,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from base.deploy.tests.containers import free_port

    with running_pooler(home) as pooler:
        # meiva:note
        # The scanner sees an exact owned pooler but its pidfile is absent. No stop signal can
        # be justified; the pooler must stay untouched.
        (pooler.directory / "pgbouncer.pid").unlink()
        config = replace(local_plane, redis_url=f"redis://127.0.0.1:{free_port()}")
        with pytest.raises(RuntimeError, match="unrecorded or replacement"):
            stop_plane(config, 1, operator_pipeline)
        assert pooler.identity.live()


def test_redis_cleanup_cannot_turn_deadline_into_an_unbounded_wait(
    local_plane: DataPlaneConfig,
    monkeypatch: pytest.MonkeyPatch,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    import asyncio

    from base.deploy.tests.containers import free_port

    class HangingClose:
        connection = None

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def aclose(self) -> None:
            await asyncio.Future()

    # meiva:note
    # A client whose close never returns is a fault no real Redis produces on demand.
    monkeypatch.setattr(plane, "Redis", HangingClose)
    config = replace(local_plane, redis_url=f"redis://127.0.0.1:{free_port()}")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        stop_plane(config, 0.1, operator_pipeline)
    assert time.monotonic() - started < 0.7


def _nonloopback_addr() -> str | None:
    """This machine's default-route address, or None when it has none.

    A UDP connect() picks the route without sending a packet (TEST-NET
    destination; connectionless sockets never contact it) — the portable read
    of "which local address would reach the network".
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addr = str(probe.getsockname()[0])
    except OSError:
        return None
    return addr if not addr.startswith("127.") else None


def _launch_incident_shape_pooler(
    home: Path,
    *,
    command_machine: MachineIdentity,
) -> tuple[int, str, str, int]:
    """Start a real pooler on the incident's degraded-path config.

    Returns (port, role, secret, pid). Skips the calling test when this host
    cannot run the fixture. Walks the reachable-address code path the incident
    belongs to: a cluster with a declared reachable address binds loopback + that
    address, so a listener-less pooler reads as degraded (a loopback host
    short-circuits to the reload path by design). Pins the pooler's home and the address to
    private, bindable values.
    """
    from base.deploy.tests.containers import free_port, wait_port

    binary = base_pooler.pgbouncer_bin()
    if not (Path(binary).exists() or shutil.which(binary)):
        pytest.skip("native pgbouncer is not installed")
    addr = _nonloopback_addr()
    if addr is None:
        pytest.skip("this host has no non-loopback address for the reachable bind")

    command_machine.set(host=addr)

    port = free_port()
    role = pb.POOLER_ADMIN
    secret = "s3cr3t"  # noqa: S105 — private ephemeral fixture
    pb.write_config(
        pg_port=15433,
        listen_port=port,
        db_name="meiva_maintenance_test",
        userlist=f'"{role}" "{secret}"\n'.encode(),
        machine=command_machine,
        max_client_conn=2000,
    )
    subprocess.run(
        [binary, "-d", str(base_pooler.ini_path())], check=True, capture_output=True, timeout=5
    )
    try:
        wait_port(port, timeout=5)
        return port, role, secret, _wait_pidfile()
    except BaseException:
        _kill_test_poolers()
        raise


def _wait_pidfile(timeout: float = 5.0) -> int:
    """The pooler pid once its pidfile lands.

    The daemon writes the pidfile asynchronously from binding its listeners:
    reading it straight after `wait_port` returned races startup (2026-09-12,
    the run that leaked the pooler). Missing or empty content is publication
    still in progress; nonempty invalid content is an error, never readiness.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if base_pooler.pidfile_path().exists():
            text = base_pooler.pidfile_path().read_text().strip()
            if text:
                return int(text)
        time.sleep(0.05)
    raise AssertionError("the pooler never wrote its pidfile")


def test_pidfile_wait_refuses_nonempty_corrupt_publication(home: Path) -> None:
    record = base_pooler.pidfile_path()
    record.write_text("not-a-native-pid\n")
    with pytest.raises(ValueError, match="invalid literal"):
        _wait_pidfile(timeout=1)


def _pidfile_pid() -> set[int]:
    """The pid recorded in the (test-home) pooler pidfile, when readable."""
    pidfile = base_pooler.pidfile_path()
    if not pidfile.exists():
        return set()
    with contextlib.suppress(ValueError):
        return {int(pidfile.read_text().strip())}
    return set()


def _kill_test_poolers(*pids: int | None) -> None:
    """Kill exactly the test-owned pooler instances, half-started ones included."""
    for target in sorted({p for p in pids if p} | _pidfile_pid()):
        if psutil.pid_exists(target):
            with contextlib.suppress(psutil.NoSuchProcess):
                proc = psutil.Process(target)
                proc.kill()
                proc.wait(timeout=5)


def test_real_start_retains_pooler_with_held_client(
    db_url: str,
    home: Path,
    *,
    command_machine: MachineIdentity,
) -> None:
    """A normal start cannot force a pooler past its held drain."""
    import psycopg

    port, role, secret, pid = _launch_incident_shape_pooler(home, command_machine=command_machine)
    old = OwnedProcess.capture(psutil.Process(pid))
    config = (base_pooler.ini_path().read_bytes(), pb.userlist_path().read_bytes())
    try:
        with psycopg.connect(
            f"postgresql://{role}:{secret}@127.0.0.1:{port}/pgbouncer", autocommit=True
        ) as client:
            assert client.execute("SHOW VERSION").fetchone() is not None
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not base_pooler.pgbouncer_public_listener_reachable(
                    port, role, machine=command_machine
                ):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("the pooler never closed its listeners after SIGTERM")
            assert old.live()
            with pytest.raises(RuntimeError, match="custody retained"):
                pb.ensure_pgbouncer(
                    pg_port=15433,
                    listen_port=port,
                    db_name="meiva_maintenance_test",
                    userlist=f'"{role}" "{secret}"\n'.encode(),
                    admin_password=secret,
                    machine=command_machine,
                    max_client_conn=2000,
                )
            assert old.live(), "normal start must retain a pooler still draining its client"
            assert pb.running_pid() == pid, "no replacement may be launched"
            assert (base_pooler.ini_path().read_bytes(), pb.userlist_path().read_bytes()) == config
    finally:
        _kill_test_poolers(pid)
