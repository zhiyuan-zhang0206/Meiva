# meiva:note
# Bringing a real pgbouncer up, reloading it and restarting it, against the real process.
#
# The decisions (reload, restart, wait, announce) are asserted as plans in
# `pooler/test_pooler_convergence.py`; here the shell executes them on a real pooler under a
# private home: its files are written 0600, it answers on its admin console, an unchanged one
# keeps its process, a changed userlist or a lost reachable listener gets a new one, and a
# listener or pidfile the home's custody cannot account for refuses before anything is written.
#
# pgbouncer treats a failed bind on one `listen_addr` entry as a warning and keeps running on the
# rest. That is what degraded a boot-time pooler to loopback-only for two days (task #1288,
# 2026-08-16), and here a pooler started without the reachable address is the degraded one.

from __future__ import annotations

import os
import signal
import socket
import stat
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import psutil
import psycopg
import pytest

from base.cluster import port_preflight
from base.cluster.dataplane import pooler as pooler_files
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.deploy.tests.containers import free_port
from base.host.db_authority.delivery import POOLER_ADMIN
from base.native_process.ownership import OwnedProcess
from cli.commands.data_plane import pgbouncer
from cli.commands.data_plane.tests.native_processes import (
    ADMIN_PASSWORD,
    kill_pooler,
    owned_pooler,
    pooler_spec,
    sleeping_child,
)
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


@pytest.fixture(autouse=True)
def reaped_pooler(unit_home: Path, postgres_access: None) -> Iterator[None]:
    """The pooler's admin console speaks the Postgres protocol, so every test here is admitted
    to it; whatever pooler a test left under its home is killed with the test."""
    del postgres_access
    yield
    kill_pooler(unit_home / "pgbouncer")


# meiva:note
# The address a packet to the outside would leave from, or None without a route. A UDP connect()
# picks the route without sending anything (TEST-NET destination).
def _route_address() -> str | None:
    try:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
            probe.connect(("192.0.2.1", 9))
            return str(probe.getsockname()[0])
    except OSError:
        return None


def _bindable(address: str) -> bool:
    try:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            probe.bind((address, 0))
    except OSError:
        return False
    return True


# meiva:note
# An address of this host that is not a loopback alias and that a pooler can bind: another
# loopback address where the whole 127/8 is local, the default-route address elsewhere.
def _assigned_reachable_address() -> str:
    for candidate in ("127.0.0.2", _route_address()):
        if candidate is not None and _bindable(candidate):
            return candidate
    raise AssertionError("this host has no address besides loopback the pooler can bind")


def test_a_fresh_start_writes_private_files_and_brings_up_a_pooler_that_answers(
    unit_home: Path, command_machine: MachineIdentityReader, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = pooler_spec(command_machine, listen_port=free_port(), socket_dir=unit_home)

    assert pgbouncer.ensure_pgbouncer(spec) == 0

    assert "✓ pgbouncer started" in capsys.readouterr().out
    owner = owned_pooler(unit_home)
    assert owner is not None and owner.live()
    assert pooler_files.admin_reachable(spec.shape.listen_port, ADMIN_PASSWORD)
    assert not pooler_files.admin_reachable(spec.shape.listen_port, "not-the-admin-password")
    for name in ("pgbouncer.ini", "userlist.txt"):
        mode = stat.S_IMODE((unit_home / "pgbouncer" / name).stat().st_mode)
        assert mode & 0o077 == 0, f"{name} is readable by others"
    assert POOLER_ADMIN in (unit_home / "pgbouncer" / "userlist.txt").read_text()


# meiva:note
# The reachable address is not up within the bound: a fresh start must NOT birth a pooler that
# would silently degrade to loopback-only. TEST-NET-1 is never assigned to an interface.
def test_a_reachable_address_that_never_appears_launches_nothing(
    unit_home: Path, command_machine: ConfiguredMachineIdentity, capsys: pytest.CaptureFixture[str]
) -> None:
    command_machine.set(host="192.0.2.1")
    spec = pooler_spec(command_machine, listen_port=free_port(), socket_dir=unit_home)

    assert pgbouncer.ensure_pgbouncer(spec) == 1

    err = capsys.readouterr().err
    assert "'192.0.2.1' is not assigned to any local interface" in err
    assert "silently degrade" in err and "private network" in err
    assert owned_pooler(unit_home) is None
    assert not (unit_home / "pgbouncer" / "pgbouncer.pid").exists()


def test_an_unchanged_running_pooler_is_reloaded_and_keeps_its_process(
    unit_home: Path, command_machine: MachineIdentityReader, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = pooler_spec(command_machine, listen_port=free_port(), socket_dir=unit_home)
    assert pgbouncer.ensure_pgbouncer(spec) == 0
    first = owned_pooler(unit_home)
    capsys.readouterr()

    assert pgbouncer.ensure_pgbouncer(spec) == 0

    assert "already running" in capsys.readouterr().out
    assert owned_pooler(unit_home) == first
    log = (unit_home / "pgbouncer" / "pgbouncer.log").read_text()
    assert "SIGHUP" in log, "the pooler was told to re-read its configuration"


# meiva:note
# A SIGHUP reload keeps a removed user that already authenticated and still admits its new
# sessions, so changed userlist bytes restart a healthy pooler; only a fresh process revokes.
def test_a_changed_userlist_restarts_the_pooler_instead_of_reloading_it(
    unit_home: Path, command_machine: MachineIdentityReader, capsys: pytest.CaptureFixture[str]
) -> None:
    port = free_port()
    assert (
        pgbouncer.ensure_pgbouncer(
            pooler_spec(command_machine, listen_port=port, socket_dir=unit_home)
        )
        == 0
    )
    first = owned_pooler(unit_home)
    assert first is not None
    capsys.readouterr()
    extra = f'"{POOLER_ADMIN}" "{ADMIN_PASSWORD}"\n"meiva_g1_runner" "other"\n'.encode()

    changed = pooler_spec(command_machine, listen_port=port, socket_dir=unit_home, userlist=extra)
    assert pgbouncer.ensure_pgbouncer(changed) == 0

    assert "a reload never revokes" in capsys.readouterr().out
    second = owned_pooler(unit_home)
    assert second is not None and second.live() and second != first
    assert not first.live(), "the old process was ended, not reloaded"
    assert b"meiva_g1_runner" in (unit_home / "pgbouncer" / "userlist.txt").read_bytes()


# meiva:note
# A pooler born without the reachable address answers on loopback, so a loopback probe reads it
# healthy. Bring-up finds its public listener missing, ends it and starts one that binds both.
def test_a_pooler_without_its_reachable_listener_is_restarted_into_a_double_bind(
    unit_home: Path, command_machine: ConfiguredMachineIdentity, capsys: pytest.CaptureFixture[str]
) -> None:
    port = free_port()
    assert (
        pgbouncer.ensure_pgbouncer(
            pooler_spec(command_machine, listen_port=port, socket_dir=unit_home)
        )
        == 0
    )
    degraded = owned_pooler(unit_home)
    assert degraded is not None
    capsys.readouterr()
    address = _assigned_reachable_address()
    command_machine.set(host=address)

    assert (
        pgbouncer.ensure_pgbouncer(
            pooler_spec(command_machine, listen_port=port, socket_dir=unit_home)
        )
        == 0
    )

    out = capsys.readouterr()
    assert f"NOT listening on the reachable address '{address}'" in out.err
    assert "✓ pgbouncer started" in out.out
    healthy = owned_pooler(unit_home)
    assert healthy is not None and healthy.live() and healthy != degraded
    assert not degraded.live()
    assert address in port_preflight.listener_addrs(port)


def test_a_missing_binary_fails_before_anything_is_written(
    unit_home: Path, command_machine: MachineIdentityReader, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = pooler_spec(
        command_machine,
        listen_port=free_port(),
        socket_dir=unit_home,
        binary=str(unit_home / "no-such" / "pgbouncer"),
    )

    assert pgbouncer.ensure_pgbouncer(spec) == 1

    err = capsys.readouterr().err
    assert "MEIVA_PGBOUNCER_ENABLED is set but pgbouncer is not installed" in err
    assert not (unit_home / "pgbouncer" / "pgbouncer.ini").exists()
    assert owned_pooler(unit_home) is None


@pytest.mark.parametrize(
    ("macos", "install", "other"),
    [(True, "brew install pgbouncer", "database.sh"), (False, "database.sh", "brew install")],
)
def test_the_install_hint_of_a_missing_binary_is_the_platforms_the_spec_names(
    unit_home: Path,
    command_machine: MachineIdentityReader,
    capsys: pytest.CaptureFixture[str],
    macos: bool,
    install: str,
    other: str,
) -> None:
    spec = pooler_spec(
        command_machine,
        listen_port=free_port(),
        socket_dir=unit_home,
        binary=str(unit_home / "no-such" / "pgbouncer"),
        macos=macos,
    )

    assert pgbouncer.ensure_pgbouncer(spec) == 1

    err = capsys.readouterr().err
    assert install in err and other not in err


# meiva:note
# A listener on the pooler's port that the home's custody cannot account for is a refusal
# before any file is written or signal sent: native readiness never authorizes changes to a
# foreign listener.
def test_a_foreign_listener_on_the_pooler_port_refuses_before_any_file_is_written(
    unit_home: Path, command_machine: MachineIdentityReader
) -> None:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        spec = pooler_spec(
            command_machine, listen_port=foreign.getsockname()[1], socket_dir=unit_home
        )

        with pytest.raises(RuntimeError, match="no owned data-plane process"):
            pgbouncer.ensure_pgbouncer(spec)

    assert not (unit_home / "pgbouncer" / "pgbouncer.ini").exists()
    assert owned_pooler(unit_home) is None


# meiva:note
# The pidfile holds a bare pid, and a stale one eventually names a recycled process. Untreated
# that wedges the cluster: bring-up would read the live number as "already running", SIGHUP a
# stranger, start nothing and never rewrite the pidfile. This process is the stranger: a real
# live pid that is definitely not a pgbouncer.
def test_a_recycled_pid_in_the_pidfile_is_refused_before_any_file_is_written(
    unit_home: Path, command_machine: MachineIdentityReader
) -> None:
    pidfile = unit_home / "pgbouncer" / "pgbouncer.pid"
    pidfile.parent.mkdir()
    pidfile.write_text(str(os.getpid()))
    spec = pooler_spec(command_machine, listen_port=free_port(), socket_dir=unit_home)

    with pytest.raises(RuntimeError, match="cannot verify this home's PgBouncer"):
        pgbouncer.ensure_pgbouncer(spec)

    assert not (unit_home / "pgbouncer" / "pgbouncer.ini").exists()


# meiva:note
# Only process mechanics cross into the daemon, never configuration or a credential: the boot
# pass may have put the gateway login, the write generation and its API token into the launching
# process, and a daemon would keep them until it restarts. The environment of the running
# daemon is read back from the process table.
def test_the_pooler_daemon_is_born_with_exactly_the_environment_it_is_given(
    unit_home: Path, command_machine: MachineIdentityReader
) -> None:
    assert "MEIVA_HOME" in os.environ, "the test process carries a MEIVA_ variable to withhold"
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    spec = pooler_spec(
        command_machine, listen_port=free_port(), socket_dir=unit_home, daemon_env=env
    )

    assert pgbouncer.ensure_pgbouncer(spec) == 0

    owner = owned_pooler(unit_home)
    assert owner is not None
    seen = psutil.Process(owner.pid).environ()
    assert seen["PATH"] == "/usr/bin:/bin" and seen["LANG"] == "C.UTF-8"
    assert not [name for name in seen if name.startswith("MEIVA_")]


# meiva:note
# A reload never signals a birth other than the one observed: the pid may name a stranger now.
# The recorded birth here is a real running process whose start identity was altered, and the
# process is not signalled (SIGHUP would end it).
def test_a_reload_refuses_a_birth_that_is_not_the_recorded_one() -> None:
    with sleeping_child() as child:
        real = OwnedProcess.capture(psutil.Process(child.pid))
        replaced = OwnedProcess(
            real.pid,
            real.birth + 1,
            real.starttime + 1 if real.starttime is not None else None,
        )

        with pytest.raises(RuntimeError, match="identity changed before reload"):
            pgbouncer.reload_pooler(replaced)

        assert real.live(), "the recorded process was not signalled"


# meiva:note
# A normal start cannot force a pooler past its held drain. PgBouncer 1.23 and later runs
# SIGTERM as a shutdown that waits for every client to disconnect: it closes its listeners and
# stays alive for as long as a client is connected (here, an idle admin console). Bring-up must
# neither restart it nor write over it nor start a second one on its port; it refuses with
# custody retained, and the pooler goes on draining.
def test_a_pooler_draining_a_held_client_keeps_custody_and_is_not_replaced(
    unit_home: Path, command_machine: MachineIdentityReader
) -> None:
    port = free_port()
    spec = pooler_spec(command_machine, listen_port=port, socket_dir=unit_home)
    assert pgbouncer.ensure_pgbouncer(spec) == 0
    old = owned_pooler(unit_home)
    assert old is not None
    files = (
        (unit_home / "pgbouncer" / "pgbouncer.ini").read_bytes(),
        pgbouncer.userlist_path().read_bytes(),
    )
    console = f"postgresql://{POOLER_ADMIN}:{ADMIN_PASSWORD}@127.0.0.1:{port}/pgbouncer"

    with psycopg.connect(console, autocommit=True) as held:
        assert held.execute("SHOW VERSION").fetchone() is not None
        assert old.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 5
        while port_preflight.strict_listeners_on(port):
            assert time.monotonic() < deadline, "the pooler never closed its listeners"
            time.sleep(0.05)

        with pytest.raises(RuntimeError, match="custody retained"):
            pgbouncer.ensure_pgbouncer(spec)

        assert old.live(), "a normal start must retain a pooler still draining its client"
        assert pgbouncer.running_pid() == old.pid, "no replacement may be launched"
        assert (
            (unit_home / "pgbouncer" / "pgbouncer.ini").read_bytes(),
            pgbouncer.userlist_path().read_bytes(),
        ) == files
