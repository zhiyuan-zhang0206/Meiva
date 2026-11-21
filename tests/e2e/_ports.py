from __future__ import annotations

import atexit
import contextlib
import random
import socket
import subprocess
import time

import psutil

from base.host.env.port_lease import lease_ports

HEALTH_PORT_KINDS = ("ops", "agent-host", "schedule-manager", "ttl-reaper")


class PortReservation:
    def __init__(self, sock: socket.socket) -> None:
        self._sock: socket.socket | None = sock
        self.port: int = sock.getsockname()[1]

    # meiva:note
    # Hand the socket to a child; the host-wide lease lasts until this process exits.
    def release(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()


def reserve_port() -> PortReservation:
    port = lease_ports(1, rng=random.SystemRandom())[0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as error:
        sock.close()
        raise RuntimeError(
            f"Cannot reserve E2E port {port}: {error}; listener: {_listener_owners(port)}"
        ) from error
    return PortReservation(sock)


def reserve_health_port(kind: str) -> PortReservation:
    if kind not in HEALTH_PORT_KINDS:
        raise ValueError(f"unknown health-port kind {kind!r}; known: {HEALTH_PORT_KINDS}")
    return reserve_port()


def _owns_listener(process: psutil.Process, port: int) -> bool:
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        return any(
            row.laddr and row.laddr.port == port and row.status == psutil.CONN_LISTEN
            for row in process.net_connections(kind="tcp")
        )
    return False


def _listener_owners(port: int) -> str:
    owners: list[str] = []
    for process in psutil.process_iter():
        if _owns_listener(process, port):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                owners.append(f"pid={process.pid} ({process.name()})")
    return ", ".join(owners) or "owner unavailable (native process visibility is restricted)"


def wait_for_port(
    host: str,
    port: int,
    timeout: float = 30.0,
    *,
    process: subprocess.Popen[str],
    label: str,
) -> None:
    # meiva:note
    # TCP success alone could be a predecessor or another run; require native ownership.
    deadline = time.monotonic() + timeout
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(
                f"{label} ({host}:{port}) pid={process.pid} exited with code {code}; "
                f"listener: {_listener_owners(port)}"
            )
        try:
            with socket.create_connection((host, port), timeout=1.0):
                pass
        except OSError as error:
            last_err = error
            time.sleep(0.1)
            continue
        try:
            root = psutil.Process(process.pid)
            candidates = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess:
            continue
        if any(_owns_listener(candidate, port) for candidate in candidates):
            return
        raise RuntimeError(
            f"{label} ({host}:{port}) is not owned by pid={process.pid} or its descendants; "
            f"listener: {_listener_owners(port)}"
        )
    raise RuntimeError(
        f"Waiting for {label} ({host}:{port}) pid={process.pid} timed out after {timeout}s; "
        f"last error: {last_err!r}"
    )


GATEWAY_SOCKET = socket.socket()
GATEWAY_SOCKET.bind(("127.0.0.1", 0))
atexit.register(GATEWAY_SOCKET.close)
GATEWAY_PORT = GATEWAY_SOCKET.getsockname()[1]
GATEWAY_URL = f"http://localhost:{GATEWAY_PORT}"

FRONTEND_RESERVATION = reserve_port()
atexit.register(FRONTEND_RESERVATION.release)
FRONTEND_PORT = FRONTEND_RESERVATION.port
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
