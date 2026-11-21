from __future__ import annotations

import contextlib
import errno
import json
import os
import socket
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from base.host.env.port_lease import BAND_FIRST, BAND_LAST, leased_ports
from base.sessions.tests.poll_until import poll_until
from tests.e2e._ports import HEALTH_PORT_KINDS, reserve_health_port, reserve_port, wait_for_port
from tests.e2e.process_support import managed_proc

_CHILD_MODULE = "tests.e2e.fakes.port_server"


def _node_like_listen(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


def test_socket_handoff_retains_the_session_lease() -> None:
    reservation = reserve_port()
    try:
        assert reservation.port in leased_ports()
        assert BAND_FIRST <= reservation.port <= BAND_LAST
        with pytest.raises(OSError) as taken:
            _node_like_listen(reservation.port)
        assert taken.value.errno == errno.EADDRINUSE
    finally:
        reservation.release()
    with contextlib.closing(_node_like_listen(reservation.port)) as listener:
        assert listener.getsockname()[1] == reservation.port
    reservation.release()
    assert reservation.port in leased_ports()


def test_released_socket_is_not_reallocated_within_the_session() -> None:
    ports: list[int] = []
    for _ in range(32):
        reservation = reserve_port()
        ports.append(reservation.port)
        reservation.release()
    assert len(set(ports)) == len(ports)
    assert set(ports) <= set(leased_ports())


def test_all_health_kinds_and_frontends_share_one_unique_lease_authority() -> None:
    reservations = [reserve_port(), *(reserve_health_port(kind) for kind in HEALTH_PORT_KINDS)]
    try:
        ports = [reservation.port for reservation in reservations]
        assert len(set(ports)) == len(ports)
        assert set(ports) <= set(leased_ports())
    finally:
        for reservation in reservations:
            reservation.release()


def test_an_unknown_health_port_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown health-port kind"):
        reserve_health_port("gateway")


@contextmanager
def _listener() -> Generator[socket.socket]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock


def test_foreign_listener_is_rejected_with_its_owner_without_waiting() -> None:
    with (
        _listener() as sock,
        managed_proc(
            [sys.executable, "-m", _CHILD_MODULE, "hold"], label="non-listener"
        ) as process,
    ):
        port = sock.getsockname()[1]
        with pytest.raises(RuntimeError, match=rf"{port}.*pid={os.getpid()}") as error:
            wait_for_port("127.0.0.1", port, process=process, timeout=10, label="collision")
        assert "not owned" in str(error.value)


@pytest.mark.parametrize("descendant", [False, True])
def test_readiness_requires_the_started_process_or_its_child(descendant: bool) -> None:
    with _listener() as sock:
        port = sock.getsockname()[1]
    command = "descendant" if descendant else "listen"
    with managed_proc(
        [sys.executable, "-m", _CHILD_MODULE, command, str(port)], label="listener"
    ) as process:
        wait_for_port("127.0.0.1", port, process=process, timeout=5, label="listener")
        assert process.poll() is None


def test_child_exit_is_reported_without_a_readiness_timeout() -> None:
    with _listener() as sock:
        port = sock.getsockname()[1]
    with (
        managed_proc([sys.executable, "-m", _CHILD_MODULE, "exit"], label="dead") as process,
        pytest.raises(RuntimeError, match="exited with code 17"),
    ):
        wait_for_port("127.0.0.1", port, process=process, timeout=10, label="dead")


def test_live_process_without_a_listener_times_out() -> None:
    with _listener() as sock:
        port = sock.getsockname()[1]
    with (
        managed_proc([sys.executable, "-m", _CHILD_MODULE, "hold"], label="silent") as process,
        pytest.raises(RuntimeError, match="timed out"),
    ):
        wait_for_port("127.0.0.1", port, process=process, timeout=0.1, label="silent")


@pytest.mark.parametrize("workers", [("", ""), ("gw0", "gw0"), ("gw0", "gw1")])
def test_parallel_sessions_lease_distinct_ports_after_socket_handoff(
    tmp_path: Path, workers: tuple[str, str]
) -> None:
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    with (
        managed_proc(
            [sys.executable, "-m", _CHILD_MODULE, "lease", str(first)],
            env={**os.environ, "PYTEST_XDIST_WORKER": workers[0]},
            label="first-shard",
        ),
        managed_proc(
            [sys.executable, "-m", _CHILD_MODULE, "lease", str(second)],
            env={**os.environ, "PYTEST_XDIST_WORKER": workers[1]},
            label="second-shard",
        ),
    ):
        poll_until(
            lambda: (first.exists() and second.exists(), "both shard port snapshots"),
            timeout=10,
            interval=0.1,
            what="concurrent lease holders",
        )
        first_ports, second_ports = json.loads(first.read_text()), json.loads(second.read_text())
        assert len(first_ports) == len(set(first_ports)) == 4
        assert len(second_ports) == len(set(second_ports)) == 4
        assert set(first_ports).isdisjoint(second_ports)
