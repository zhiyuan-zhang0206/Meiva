from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from services.redis_bridge import relay

__all__ = [
    "BlockedDials",
    "GatedAddress",
    "SocketDefect",
    "backend_listener",
    "blocked_dials",
    "close_all",
    "occupied_address",
    "wait_bound",
    "wait_until",
]


# meiva:note
# Poll `predicate` until it holds; a timeout is a test failure with the given description.
def wait_until(predicate: Callable[[], bool], what: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


# meiva:note
# The address the service's current listener is bound to, once it differs from `previous`.
def wait_bound(
    service: relay.RelayService, previous: tuple[str, int] | None = None
) -> tuple[str, int]:
    found: list[tuple[str, int]] = []

    def bound() -> bool:
        address = service.bound_address()
        if address is not None and address != previous:
            found.append(address)
        return bool(found)

    wait_until(bound, "a bound relay listener")
    return found[0]


# meiva:note
# A loopback listener standing in for Redis, accepting within two seconds.
@contextmanager
def backend_listener() -> Generator[socket.socket]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2.0)
        yield listener


# meiva:note
# An address a relay cannot bind because another socket is listening on it.
@contextmanager
def occupied_address() -> Generator[tuple[str, int]]:
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        name = holder.getsockname()
        yield (name[0], name[1])


# meiva:note
# Shut down and close every endpoint, ignoring ones the relay already closed.
def close_all(*connections: socket.socket) -> None:
    for connection in connections:
        with suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        connection.close()


# meiva:note
# A backend address whose unpacking, the first thing a dial does, waits at a gate. It stands
# in for a resolver that stalls, and it can fail with an unknown error when released, the
# shape of a resolver implementation defect.
class GatedAddress(tuple[str, int]):
    entered: threading.Event
    release: threading.Event
    fail: BaseException | None

    def __new__(
        cls,
        host: str,
        port: int,
        *,
        entered: threading.Event,
        release: threading.Event,
        fail: BaseException | None = None,
    ) -> GatedAddress:
        address = super().__new__(cls, (host, port))
        address.entered = entered
        address.release = release
        address.fail = fail
        return address

    def __iter__(self) -> Iterator[str | int]:
        self.entered.set()
        if not self.release.wait(2.0):
            raise AssertionError("the gate was never released")
        if self.fail is not None:
            raise self.fail
        return iter((self[0], self[1]))


# meiva:note
# A socket-shaped endpoint over a real socket with scripted defects: `recv` can wait for a gate
# and then raise, `sendall` can raise, and `shutdown` closes the real socket and then raises
# when a worker thread (not the test's thread) calls it.
class SocketDefect:
    def __init__(
        self,
        connection: socket.socket,
        *,
        recv_error: BaseException | None = None,
        recv_gate: threading.Event | None = None,
        sendall_error: BaseException | None = None,
        worker_shutdown_error: BaseException | None = None,
    ) -> None:
        self.connection = connection
        self.recv_error = recv_error
        self.recv_gate = recv_gate
        self.sendall_error = sendall_error
        self.worker_shutdown_error = worker_shutdown_error

    def recv(self, size: int) -> bytes:
        if self.recv_error is None:
            return self.connection.recv(size)
        if self.recv_gate is not None and not self.recv_gate.wait(2.0):
            raise AssertionError("the receive gate was never released")
        raise self.recv_error

    def sendall(self, data: bytes) -> None:
        if self.sendall_error is not None:
            raise self.sendall_error
        self.connection.sendall(data)

    def shutdown(self, how: int) -> None:
        with suppress(OSError):
            self.connection.shutdown(how)
        if (
            self.worker_shutdown_error is not None
            and threading.current_thread() is not threading.main_thread()
        ):
            raise self.worker_shutdown_error

    def close(self) -> None:
        self.connection.close()


# meiva:note
# A service whose owners are all stalled in their dial: `release` lets every dial through to
# the real backend listener, `client_pairs` are the socketpairs the owners were admitted from.
@dataclass
class BlockedDials:
    service: relay.RelayService
    owners: tuple[relay.Connection, ...]
    client_pairs: list[tuple[socket.socket, socket.socket]]
    backend_side: socket.socket
    entered: threading.Event
    release: threading.Event


# meiva:note
# Admit `count` connections to a service whose backend dial stalls until `release` is set.
@contextmanager
def blocked_dials(count: int) -> Generator[BlockedDials]:
    entered, release = threading.Event(), threading.Event()
    with backend_listener() as backend_side:
        host, port = backend_side.getsockname()
        gated = GatedAddress(host, port, entered=entered, release=release)
        service = relay.RelayService(("127.0.0.1", 0), gated)
        pairs = [socket.socketpair() for _ in range(count)]
        for _client, accepted in pairs:
            service._admit(accepted, ("127.0.0.1", 1))
        try:
            yield BlockedDials(
                service, tuple(service._connections), pairs, backend_side, entered, release
            )
        finally:
            release.set()
            service.stop(2.0)
            close_all(*(endpoint for pair in pairs for endpoint in pair))
