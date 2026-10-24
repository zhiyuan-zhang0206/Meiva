# meiva:note
# Real data-plane processes for the pooler and maintenance-stop tests: a pgbouncer whose files
# are a home's pooler files, a redis-server on a data directory, and the specs a bring-up is
# handed.
#
# A test that needs a pooler or a Redis starts the real one under its home instead of replacing
# the code that looks for it. The pooler binary and `redis-server` are assertions, not skips:
# the suite's CI image installs both.

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import time
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import psycopg
import pytest

from base.cluster import ownership
from base.cluster.dataplane import pooler as pooler_files
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.cluster.dataplane.pg_tools import throwaway_postgres
from base.cluster.machine import MachineIdentity
from base.deploy.tests.containers import free_port, wait_port
from base.host.db_authority.delivery import POOLER_ADMIN
from base.native_process.ownership import OwnedProcess
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.pgbouncer import PoolerShape, PoolerSpec, binary_installed

__all__ = [
    "ADMIN_PASSWORD",
    "Pooler",
    "Redis",
    "kill_pooler",
    "native_pooler",
    "owned_pooler",
    "pooler_binary",
    "pooler_spec",
    "running_pooler",
    "running_redis",
    "sleeping_child",
]

# meiva:note
# The console password of a test pooler: a fixture value, not a secret.
ADMIN_PASSWORD = "pooler-admin-fixture-password"  # noqa: S105

_CHILD = Path(__file__).with_name("_sleeping_child.py")

# meiva:note
# The daemon environment of a test pooler: process mechanics only, as the composition root
# hands a bring-up.
_DAEMON_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


@dataclass(frozen=True)
class Pooler:
    port: int
    pid: int
    identity: OwnedProcess
    directory: Path


@dataclass(frozen=True)
class Redis:
    url: str
    port: int
    pid: int
    data: Path


def pooler_binary() -> str:
    binary = pooler_files.pgbouncer_bin()
    assert binary_installed(binary), "the suite's CI image installs pgbouncer"
    return binary


# meiva:note
# A bring-up of the pooler of `machine` on `listen_port`, with the waits small enough for a
# test. The userlist carries the admin console entry whose password is `admin_password`, which
# pgbouncer derives its SCRAM secret from; a test that needs more entries passes its own bytes.
def pooler_spec(
    machine: MachineIdentity,
    *,
    listen_port: int,
    socket_dir: Path,
    userlist: bytes | None = None,
    admin_password: str = ADMIN_PASSWORD,
    binary: str | None = None,
    daemon_env: Mapping[str, str] = _DAEMON_ENV,
    macos: bool = False,
    bind_wait_s: float = 0.3,
    stop_wait_s: float = 5.0,
    ready_wait_s: float = 6.0,
) -> PoolerSpec:
    return PoolerSpec(
        shape=PoolerShape(
            pg_port=15433,
            listen_port=listen_port,
            db_name="meiva_pooler_test",
            machine=machine,
            max_client_conn=2000,
            socket_dir=socket_dir,
        ),
        userlist=userlist
        if userlist is not None
        else f'"{POOLER_ADMIN}" "{admin_password}"\n'.encode(),
        admin_password=admin_password,
        binary=binary if binary is not None else pooler_binary(),
        daemon_env=daemon_env,
        macos=macos,
        bind_wait_s=bind_wait_s,
        stop_wait_s=stop_wait_s,
        ready_wait_s=ready_wait_s,
    )


def _wait_pidfile(pidfile: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pidfile.exists() and (text := pidfile.read_text().strip()):
            return int(text)
        time.sleep(0.05)
    raise AssertionError("the pooler never wrote its pidfile")


# meiva:note
# Kill the pooler that `directory`'s files name, if it really is that pooler. A pidfile that
# names anything else (a test plants this very process there) is left alone: the test runner is
# not a pooler to kill.
def kill_pooler(directory: Path) -> None:
    try:
        identity = ownership.pooler(directory / "pgbouncer.ini", directory / "pgbouncer.pid")
    except RuntimeError:
        return
    if identity is not None and identity.live():
        process = psutil.Process(identity.pid)
        process.kill()
        with contextlib.suppress(psutil.NoSuchProcess):
            process.wait(timeout=5)


# meiva:note
# A real pgbouncer whose files are the pooler files of `home`, started as an operator would:
# `pgbouncer -d <ini>` from where the launcher stood, or from the config directory with the bare
# file name (`relative_ini`), which is the macOS command-line shape.
#
# With `console_password` the admin console authenticates with SCRAM as `meiva_pooler_admin`;
# without it the console trusts everyone. The pooler is killed on exit if it is still alive.
@contextmanager
def running_pooler(
    home: Path,
    *,
    relative_ini: bool = False,
    console_password: str | None = None,
    backend_port: int | None = None,
) -> Generator[Pooler]:
    directory = home / "pgbouncer"
    directory.mkdir(parents=True, exist_ok=True)
    port = free_port()
    admin = POOLER_ADMIN if console_password is not None else "anyone"
    users = [f'"{admin}" "{console_password or ""}"']
    databases = "[databases]\n"
    pooling = ""
    if backend_port is not None:
        users = ['"meiva" ""', f'"{POOLER_ADMIN}" ""']
        databases += f"test = host=127.0.0.1 port={backend_port} dbname=meiva_citest user=meiva\n"
        pooling = "pool_mode=transaction\n"
        admin = f"meiva,{POOLER_ADMIN}"
    (directory / "userlist.txt").write_text("\n".join(users) + "\n")
    ini = directory / "pgbouncer.ini"
    ini.write_text(
        f"{databases}[pgbouncer]\nlisten_addr=127.0.0.1\n"
        f"listen_port={port}\n"
        f"auth_type={'scram-sha-256' if console_password is not None else 'trust'}\n"
        f"auth_file={directory / 'userlist.txt'}\n{pooling}"
        f"admin_users={admin}\nstats_users={admin}\n"
        f"pidfile={directory / 'pgbouncer.pid'}\n"
        f"logfile={directory / 'pgbouncer.log'}\n"
        "unix_socket_dir=\n"
    )
    argv = [pooler_binary(), "-d", "pgbouncer.ini" if relative_ini else str(ini)]
    subprocess.run(argv, check=True, capture_output=True, timeout=10, cwd=directory)
    try:
        wait_port(port, timeout=5)
        pid = _wait_pidfile(directory / "pgbouncer.pid")
        yield Pooler(port, pid, OwnedProcess.capture(psutil.Process(pid)), directory)
    finally:
        kill_pooler(directory)


# meiva:note
# A real redis-server whose data directory is `directory`: what the stop must be able to own.
@contextmanager
def running_redis(directory: Path, *, password: str | None = None) -> Generator[Redis]:
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


# meiva:note
# The identity of the pooler a home's files name, or None.
def owned_pooler(home: Path) -> OwnedProcess | None:
    directory = home / "pgbouncer"
    return ownership.pooler(directory / "pgbouncer.ini", directory / "pgbouncer.pid")


# meiva:note
# A live process of the test's own to point at, killed on exit: `_sleeping_child.py`, which
# sleeps. With `undumpable` (Linux) its working directory is unreadable to other processes of
# its user, and the child says so once it is.
@contextmanager
def sleeping_child(*, undumpable: bool = False) -> Generator[subprocess.Popen[str]]:
    argv = [sys.executable, str(_CHILD), *(["undumpable"] if undumpable else [])]
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    try:
        if undumpable:
            assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        yield child
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.fixture
def native_pooler(
    unit_home: Path, postgres_access: None, throwaway_config: ThrowawayConfig
) -> Iterator[tuple[OwnedPooler, str, str]]:
    """A real pgbouncer under the test's home, in front of a real throwaway Postgres.

    Yields the custodian of the pooler, a URL that dials the database through it and a URL that
    dials the database directly."""
    del postgres_access
    with throwaway_postgres(config=throwaway_config) as direct:
        backend_port = urlsplit(direct).port
        assert backend_port is not None
        with running_pooler(unit_home, backend_port=backend_port) as running:
            with psycopg.connect(direct, autocommit=True) as conn:
                conn.execute("CREATE TABLE pooler_stop_receipt (value integer)")
            yield (
                OwnedPooler(running.identity, running.port, running.directory / "pgbouncer.ini"),
                f"postgresql://meiva@127.0.0.1:{running.port}/test",
                direct,
            )
