from __future__ import annotations

import math
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import cast

import pytest

from services.redis_bridge import relay
from services.redis_bridge.tests.relay_support import (
    GatedAddress,
    SocketDefect,
    backend_listener,
    blocked_dials,
    close_all,
    occupied_address,
    wait_bound,
    wait_until,
)

_OVERLONG_LABEL_HOST = "a" * 64 + ".example"


def test_serving_exit_reclaims_an_accepted_idle_connection() -> None:
    existing_threads = set(threading.enumerate())
    with backend_listener() as backend_side, occupied_address() as unbindable:
        client, accepted = socket.socketpair()
        client.settimeout(2.0)
        service = relay.RelayService(unbindable, backend_side.getsockname(), rebind_delay_s=0.0)
        service._admit(accepted, ("127.0.0.1", 1))
        backend, _ = backend_side.accept()
        try:
            backend.sendall(b"ready")
            assert client.recv(5) == b"ready"
            with pytest.raises(relay.ListenerExhaustedError, match="5 times in a row"):
                relay.run_service(service)
            assert accepted.fileno() == -1
            assert client.recv(1) == b""
        finally:
            close_all(client, accepted, backend)
            for worker in set(threading.enumerate()) - existing_threads:
                worker.join(timeout=2.0)
                assert not worker.is_alive(), "serving exit must reclaim its workers"


def test_stop_fences_admission_and_reclaims_idle_pumps() -> None:
    with backend_listener() as backend_side:
        service = relay.RelayService(("127.0.0.1", 0), backend_side.getsockname())
        client, accepted = socket.socketpair()
        service._admit(accepted, ("127.0.0.1", 1))
        backend, _ = backend_side.accept()
        owners = tuple(service._connections)
        try:
            backend.sendall(b"ready")
            client.settimeout(2.0)
            assert client.recv(5) == b"ready"
            assert service.stop(2.0)
            assert all(owner.completed() for owner in owners)
            assert not service._connections
            refused, refused_accepted = socket.socketpair()
            try:
                service._admit(refused_accepted, ("127.0.0.1", 2))
                assert refused_accepted.fileno() == -1
                assert not service._connections
            finally:
                close_all(refused, refused_accepted)
        finally:
            close_all(client, backend)
            service.stop(2.0)


def test_one_stop_budget_retains_blocked_dns_then_reaps_late_backend(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with blocked_dials(2) as stalled:
        assert stalled.entered.wait(2.0)
        before = time.monotonic()
        assert not stalled.service.stop(0.08)
        assert time.monotonic() - before < 0.3
        assert len(stalled.service._connections) == 2
        assert "2 connection owner(s) retained" in capsys.readouterr().out
        assert all(accepted.fileno() == -1 for _, accepted in stalled.client_pairs)
        stalled.release.set()
        assert stalled.service.stop(2.0)
        assert all(owner.completed() and not owner._pumps for owner in stalled.owners)
        for _ in stalled.owners:
            peer, _ = stalled.backend_side.accept()
            with peer:
                peer.settimeout(2.0)
                assert peer.recv(1) == b""


def test_late_unknown_error_is_visible_and_collected_by_the_original_service(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    error = ValueError("late resolver implementation defect")
    gated = GatedAddress("redis.example", 6380, entered=entered, release=release, fail=error)
    service = relay.RelayService(("127.0.0.1", 0), gated)
    client, accepted = socket.socketpair()
    service._admit(accepted, ("127.0.0.1", 1))
    owner = service._connections[0]
    try:
        assert entered.wait(2.0)
        assert not service.stop(0.01)
        release.set()
        assert owner._done.wait(2.0)
        assert "late resolver implementation defect" in capsys.readouterr().err
        with pytest.raises(ValueError) as collected:
            service.stop(2.0)
        assert collected.value is error
        assert not service._connections
        assert owner.completed()
    finally:
        release.set()
        with suppress(ValueError):
            service.stop(2.0)
        close_all(client, accepted)


def test_unknown_pump_error_interrupts_reverse_and_is_raised_by_service() -> None:
    error = LookupError("forwarding implementation defect")
    with backend_listener() as backend_side:
        client, accepted = socket.socketpair()
        service = relay.RelayService(("127.0.0.1", 0), backend_side.getsockname())
        service._admit(
            cast(socket.socket, SocketDefect(accepted, recv_error=error)), ("127.0.0.1", 1)
        )
        owner = service._connections[0]
        backend, _ = backend_side.accept()
        try:
            assert owner._done.wait(2.0), "unknown pump failure must release the reverse pump"
            owner._thread.join(timeout=2.0)
            service._reap()
            assert not service._connections
            with pytest.raises(LookupError) as collected:
                service.stop(2.0)
            assert collected.value is error
            assert owner.completed()
            assert not service._connections
        finally:
            with suppress(LookupError):
                service.stop(2.0)
            close_all(client, accepted, backend)


def test_cleanup_keeps_primary_caller_error_and_records_secondary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    release = threading.Event()
    secondary = ValueError("secondary backend defect")
    gated = GatedAddress(
        "redis.example", 6380, entered=threading.Event(), release=release, fail=secondary
    )
    client, accepted = socket.socketpair()
    with occupied_address() as unbindable:
        service = relay.RelayService(unbindable, gated, rebind_delay_s=0.0)
        service._admit(accepted, ("127.0.0.1", 1))
        release.set()
        try:
            with pytest.raises(relay.ListenerExhaustedError):
                relay.run_service(service)
            assert service._errors == [secondary]
            assert "secondary backend defect" in capsys.readouterr().err
            assert accepted.fileno() == -1
        finally:
            with suppress(ValueError):
                service.stop(2.0)
            close_all(client, accepted)


@pytest.mark.parametrize("timeout", [-1.0, math.inf, math.nan])
def test_stop_rejects_nonfinite_or_negative_budget(timeout: float) -> None:
    service = relay.RelayService(("127.0.0.1", 0), ("127.0.0.1", 6380))
    with pytest.raises(ValueError, match="finite and nonnegative"):
        service.stop(timeout)


@pytest.mark.parametrize("delay", [-1.0, math.inf, math.nan])
def test_rebind_delay_must_be_finite_and_nonnegative(delay: float) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        relay.RelayService(("127.0.0.1", 0), ("127.0.0.1", 6380), rebind_delay_s=delay)


def test_the_rebind_delay_doubles_to_its_cap() -> None:
    delays = [relay.INITIAL_REBIND_DELAY_S]
    for _ in range(6):
        delays.append(relay.next_rebind_delay(delays[-1]))
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert relay.next_rebind_delay(0.0) == 0.0


def test_rebound_listener_preserves_the_old_live_connection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with backend_listener() as backend_side:
        service = relay.RelayService(
            ("127.0.0.1", 0), backend_side.getsockname(), rebind_delay_s=0.0
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            serving = executor.submit(relay.run_service, service)
            first = wait_bound(service)
            client = socket.create_connection(first, timeout=2.0)
            backend, _ = backend_side.accept()
            try:
                client.sendall(b"old request")
                assert backend.recv(11) == b"old request"
                assert service._listener is not None
                service._listener.close()
                second = wait_bound(service, previous=first)
                client.sendall(b"old again")
                assert backend.recv(9) == b"old again"
                backend.sendall(b"old response")
                assert client.recv(12) == b"old response"
                new_client = socket.create_connection(second, timeout=2.0)
                new_backend, _ = backend_side.accept()
                try:
                    new_client.sendall(b"new")
                    assert new_backend.recv(3) == b"new"
                finally:
                    close_all(new_client, new_backend)
            finally:
                service.request_stop()
                serving.result(timeout=3.0)
                close_all(client, backend)
            assert not service._connections
    assert "listener accept failed" in capsys.readouterr().out


def test_an_admitted_connection_clears_the_listener_failure_count() -> None:
    with backend_listener() as backend_side:
        service = relay.RelayService(
            ("127.0.0.1", 0), backend_side.getsockname(), rebind_delay_s=0.0
        )
        endpoints: list[socket.socket] = []
        with ThreadPoolExecutor(max_workers=1) as executor:
            serving = executor.submit(relay.run_service, service)
            address: tuple[str, int] | None = None
            try:
                for index in range(relay.MAX_LISTENER_FAILURES + 2):
                    address = wait_bound(service, previous=address)
                    client = socket.create_connection(address, timeout=2.0)
                    backend, _ = backend_side.accept()
                    endpoints.extend((client, backend))
                    client.sendall(bytes([index]))
                    assert backend.recv(1) == bytes([index])
                    assert service._listener is not None
                    service._listener.close()
                assert not serving.done(), "an admitted connection must reset the failure count"
            finally:
                service.request_stop()
                serving.result(timeout=3.0)
                close_all(*endpoints)


def test_listener_that_never_binds_gives_up_after_the_failure_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with occupied_address() as unbindable:
        service = relay.RelayService(unbindable, ("127.0.0.1", 6380), rebind_delay_s=0.0)
        with pytest.raises(relay.ListenerExhaustedError, match="5 times in a row"):
            relay.run_service(service)
    assert capsys.readouterr().out.count("listener bind failed") == relay.MAX_LISTENER_FAILURES


def test_listener_bind_failures_back_off_until_the_address_returns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    name = holder.getsockname()
    address = (name[0], name[1])
    service = relay.RelayService(address, ("127.0.0.1", 6380), rebind_delay_s=0.05)
    with ThreadPoolExecutor(max_workers=1) as executor:
        serving = executor.submit(relay.run_service, service)
        try:
            wait_until(lambda: service._listener_failures >= 3, "three failed binds")
            assert service.bound_address() is None
            holder.close()
            assert wait_bound(service) == address
        finally:
            holder.close()
            service.request_stop()
            serving.result(timeout=3.0)
    assert "listener bind failed" in capsys.readouterr().out


def test_failed_worker_admission_closes_the_unowned_accepted_socket() -> None:
    service = relay.RelayService(("127.0.0.1", 0), ("127.0.0.1", 6380))
    client, accepted = socket.socketpair()
    previous = threading.stack_size()
    try:
        threading.stack_size(2**50)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            service._admit(accepted, ("127.0.0.1", 1))
    finally:
        threading.stack_size(previous)
    try:
        assert accepted.fileno() == -1
        assert not service._connections
        assert service.stop(0.0)
    finally:
        close_all(client, accepted)


def test_serving_reaps_completed_connections_without_stopping_admission() -> None:
    with backend_listener() as backend_side:
        client, accepted = socket.socketpair()
        client.settimeout(2.0)
        service = relay.RelayService(("127.0.0.1", 0), backend_side.getsockname())
        service._admit(accepted, ("127.0.0.1", 1))
        owner = service._connections[0]
        backend, _ = backend_side.accept()
        backend.settimeout(2.0)
        try:
            client.shutdown(socket.SHUT_WR)
            assert backend.recv(1) == b""
            backend.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
            assert owner._done.wait(2.0)
            owner._thread.join(timeout=2.0)
            service._reap()
            assert not service._connections
            assert not service._stopped.is_set()
            assert owner.completed()
        finally:
            service.stop(2.0)
            close_all(client, accepted, backend)


def test_two_pump_defects_are_both_retained_and_the_first_is_raised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = LookupError("reverse pump defect")
    second = ValueError("forward pump defect")
    gate = threading.Event()
    with backend_listener() as backend_side:
        client, accepted = socket.socketpair()
        endpoint = SocketDefect(accepted, recv_error=second, recv_gate=gate, sendall_error=first)
        service = relay.RelayService(("127.0.0.1", 0), backend_side.getsockname())
        service._admit(cast(socket.socket, endpoint), ("127.0.0.1", 1))
        owner = service._connections[0]
        backend, _ = backend_side.accept()
        try:
            backend.sendall(b"data for the defective client side")
            wait_until(lambda: service._errors == [first], "the reverse pump failure")
            gate.set()
            assert owner._done.wait(2.0)
            with pytest.raises(LookupError) as collected:
                service.stop(2.0)
            assert collected.value is first
            assert service._errors == [first, second]
            stderr = capsys.readouterr().err
            assert stderr.count(str(first)) == 1
            assert stderr.count(str(second)) == 1
            assert owner.completed()
            assert not service._connections
        finally:
            gate.set()
            with suppress(LookupError):
                service.stop(2.0)
            close_all(client, accepted, backend)


def test_worker_failure_cleanup_retains_both_original_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = LookupError("pump worker primary defect")
    secondary = ValueError("pump worker shutdown secondary defect")
    client, accepted = socket.socketpair()
    endpoint = cast(
        socket.socket,
        SocketDefect(accepted, sendall_error=primary, worker_shutdown_error=secondary),
    )
    with backend_listener() as backend_side:
        service = relay.RelayService(("127.0.0.1", 0), backend_side.getsockname())
        service._admit(endpoint, ("127.0.0.1", 1))
        owner = service._connections[0]
        backend, _ = backend_side.accept()
        try:
            backend.sendall(b"data for the defective client side")
            assert owner._done.wait(2.0)
            with pytest.raises(LookupError) as collected:
                service.stop(2.0)
            assert collected.value is primary
            assert service._errors == [primary, secondary]
            stderr = capsys.readouterr().err
            assert str(primary) in stderr and str(secondary) in stderr
            assert owner.completed()
            assert not service._connections
        finally:
            with suppress(LookupError):
                service.stop(2.0)
            close_all(client, accepted, backend)


def test_connect_failure_cleanup_preserves_primary_and_retains_secondary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secondary = ValueError("connection close secondary defect")
    client, accepted = socket.socketpair()
    endpoint = cast(socket.socket, SocketDefect(accepted, worker_shutdown_error=secondary))
    service = relay.RelayService(("127.0.0.1", 0), (_OVERLONG_LABEL_HOST, 6380))
    service._admit(endpoint, ("127.0.0.1", 1))
    owner = service._connections[0]
    try:
        assert owner._done.wait(2.0)
        with pytest.raises(UnicodeError) as collected:
            service.stop(2.0)
        primary = collected.value
        assert service._errors == [primary, secondary]
        stderr = capsys.readouterr().err
        assert str(primary) in stderr and str(secondary) in stderr
        assert owner.completed()
        assert not service._connections
    finally:
        with suppress(UnicodeError, ValueError):
            service.stop(2.0)
        close_all(client, accepted)
