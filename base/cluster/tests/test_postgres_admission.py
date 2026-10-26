# meiva:note
# A FIFO in PostgreSQL's configuration holds the real postmaster before it creates its
# pidfile. Releasing the FIFO lets the same captured native birth finish admission.
# meiva:link base/cluster/docs/postgres.meiva.okf.md

from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import psycopg
import pytest

from base.clock import SYSTEM_TIMERS
from base.clock.tests.manual_timers import ManualTimers
from base.cluster import ownership
from base.cluster import postgres as pg
from base.cluster.dataplane.pg_tools import (
    free_port,
    pg_shm_args,
    pg_start_env,
    pg_tool,
)
from base.host.net.resilience import ExponentialBackoff, Policy, retry


# meiva:note
# The resolved initdb binary receives only fixed flags and this fixture's disposable PGDATA.
@pytest.fixture
def postmaster(
    tmp_path: Path, postgres_access: None
) -> Iterator[tuple[Path, int, list[subprocess.Popen[bytes]]]]:
    """An initialized private PGDATA and retained handles for its real postmaster."""
    data = tmp_path / "pg"
    subprocess.run(  # noqa: S603
        [
            str(pg_tool("initdb")),
            "-D",
            str(data),
            "-U",
            "meiva_admission",
            "-A",
            "trust",
            "--no-sync",
            "--encoding=UTF8",
            "--locale=C",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    children: list[subprocess.Popen[bytes]] = []
    try:
        yield data, free_port(), children
    finally:
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGQUIT)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


# meiva:note
# The resolved readiness probe receives only the loopback endpoint and the kernel-chosen port.
def _ready(port: int) -> bool:
    return (
        subprocess.run(  # noqa: S603
            [str(pg_tool("pg_isready")), "-h", "127.0.0.1", "-p", str(port), "-t", "1"],
            capture_output=True,
            timeout=2,
            check=False,
        ).returncode
        == 0
    )


def _argv(data: Path, port: int) -> list[str]:
    return [
        str(pg_tool("postgres")),
        "-D",
        str(data),
        "-p",
        str(port),
        "-c",
        "listen_addresses=127.0.0.1",
        "-c",
        "unix_socket_directories=",
        *shlex.split(pg_shm_args()),
    ]


def _assert_protocol_ready(port: int) -> None:
    assert _ready(port)


def _assert_reloaded(port: int) -> None:
    assert _reloaded(port)


def _assertion_failure(exc: Exception) -> bool:
    return isinstance(exc, AssertionError)


@contextmanager
def _startup_gate(data: Path) -> Generator[None]:
    gate = data / "startup-gate.conf"
    os.mkfifo(gate, 0o600)
    with (data / "postgresql.conf").open("a") as config:
        config.write("\ninclude = 'startup-gate.conf'\n")
    held = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
    try:
        yield
    finally:
        gate.unlink()
        gate.write_text("")
        os.close(held)


# meiva:note
# The readiness deadline is the caller's clock: a postmaster that never answers is refused after
# exactly the timeout of sleeps on the caller's `Timers`, without waiting for it.
def test_a_postmaster_that_never_becomes_ready_is_refused_on_the_callers_timers(
    postmaster: tuple[Path, int, list[subprocess.Popen[bytes]]],
) -> None:
    data, port, children = postmaster
    manual = ManualTimers()
    with _startup_gate(data), pytest.raises(RuntimeError, match="did not become ready"):
        pg.start(
            data,
            port,
            _argv(data, port),
            pg_start_env(),
            ready=lambda: False,
            timeout=2.0,
            retained_children=children,
            timers=manual,
        )

    assert sum(manual.sleeps) == pytest.approx(2.0, abs=0.06)
    assert manual.monotonic() >= 2.0


def test_captured_postmaster_without_pidfile_resumes_full_admission(
    postmaster: tuple[Path, int, list[subprocess.Popen[bytes]]],
) -> None:
    data, port, children = postmaster
    argv, env, ready = _argv(data, port), pg_start_env(), partial(_ready, port)
    with _startup_gate(data):
        with pytest.raises(RuntimeError, match="did not become ready"):
            pg.start(
                data,
                port,
                argv,
                env,
                ready=ready,
                timeout=0.1,
                retained_children=children,
                timers=SYSTEM_TIMERS,
            )
        captured = pg.read_receipt(data)
        assert captured is not None and captured.state == "captured"
        owner = captured.process()
        assert owner.live() and not (data / "postmaster.pid").exists()
        assert ownership.require_postgres(data, port, required=False) == owner
        with pytest.raises(RuntimeError, match="has no listener"):
            ownership.require_postgres(data, port)
        with pytest.raises(RuntimeError, match="did not become ready"):
            pg.start(
                data,
                port,
                argv,
                env,
                ready=ready,
                timeout=0.1,
                expected=owner,
                retained_children=children,
                timers=SYSTEM_TIMERS,
            )
        assert owner.live() and pg.read_receipt(data) == captured

        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", port))
            foreign.listen()
            with pytest.raises(RuntimeError, match="does not belong"):
                ownership.require_postgres(data, port, required=False)

        record = data / "postmaster.pid"
        record.write_text(f"{owner.pid + 1}\n{data}\n100\n{port}\n")
        try:
            with pytest.raises(RuntimeError, match="does not describe"):
                pg.observe(data)
        finally:
            record.unlink()

    poll = retry(
        Policy(
            max_attempts=100,
            backoff=ExponentialBackoff(base=0.05, factor=1, cap=0.05),
            jitter="none",
            classify=_assertion_failure,
        )
    )
    poll(partial(_assert_protocol_ready, port))
    assert pg.read_receipt(data) == captured
    with (data / "postgresql.conf").open("a") as config:
        config.write("\nlog_min_messages = 'error'\n")
    assert not _reloaded(port)
    resumed = pg.start(
        data,
        port,
        argv,
        env,
        ready=ready,
        timeout=10,
        expected=owner,
        retained_children=children,
        timers=SYSTEM_TIMERS,
    )
    assert owner.same_birth(resumed)
    assert len(children) == 1
    admitted = pg.read_receipt(data)
    assert admitted is not None and admitted.state == "ready"
    assert admitted.pidfile is not None and admitted.header is not None
    assert ownership.require_postgres(data, port) == owner
    poll(partial(_assert_reloaded, port))


def test_ready_postmaster_still_requires_its_bound_pidfile(
    postmaster: tuple[Path, int, list[subprocess.Popen[bytes]]],
) -> None:
    data, port, children = postmaster
    owner = pg.start(
        data,
        port,
        _argv(data, port),
        pg_start_env(),
        ready=partial(_ready, port),
        timeout=10,
        retained_children=children,
        timers=SYSTEM_TIMERS,
    )
    record = data / "postmaster.pid"
    saved = record.rename(data / "retained.pid")
    try:
        with pytest.raises(FileNotFoundError):
            pg.observe(data)
        with pytest.raises(FileNotFoundError):
            pg.start(
                data,
                port,
                _argv(data, port),
                pg_start_env(),
                ready=partial(_ready, port),
                timeout=1,
                expected=owner,
                retained_children=children,
                timers=SYSTEM_TIMERS,
            )
    finally:
        saved.rename(record)
    assert ownership.require_postgres(data, port) == owner


def _reloaded(port: int) -> bool:
    with psycopg.connect(
        host="127.0.0.1", port=port, user="meiva_admission", dbname="postgres", connect_timeout=1
    ) as conn:
        return conn.execute("SHOW log_min_messages").fetchone() == ("error",)


def test_ready_warm_start_reloads_configuration_before_waiting_for_readiness(
    postmaster: tuple[Path, int, list[subprocess.Popen[bytes]]],
) -> None:
    data, port, children = postmaster
    owner = pg.start(
        data,
        port,
        _argv(data, port),
        pg_start_env(),
        ready=partial(_ready, port),
        timeout=10,
        retained_children=children,
        timers=SYSTEM_TIMERS,
    )
    with (data / "postgresql.conf").open("a") as config:
        config.write("\nlog_min_messages = 'error'\n")
    assert not _reloaded(port)
    resumed = pg.start(
        data,
        port,
        _argv(data, port),
        pg_start_env(),
        ready=partial(_reloaded, port),
        timeout=10,
        expected=owner,
        retained_children=children,
        timers=SYSTEM_TIMERS,
    )
    assert owner.same_birth(resumed) and len(children) == 1
    assert _reloaded(port)
