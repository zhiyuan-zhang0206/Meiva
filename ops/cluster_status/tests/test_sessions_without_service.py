# meiva:note
# What a host reports when its pty-sessions service is not there, or is there and not answering.
#
# The status snapshot must stay readable while a backend is down: a service that is absent has no
# sessions (its sessions ended with it), and one that is dialed and does not respond is logged
# and skipped. Both are real: an empty home, and a listener at the service's socket that accepts
# a connection and closes it unanswered.

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from base.api_contracts.agent_contract import ShellKillMode
from base.sessions.backend import PosixProcSessionBackend, get_shell_backend
from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import service_socket
from ops.cluster_status import (
    ShellNotFoundError,
    agent_shell_sessions,
    capture_shell,
    kill_agent_shells,
    kill_shell,
)
from ops.cluster_status.sessions import collect_sessions

pytestmark = pytest.mark.usefixtures("unit_home")


def _now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _wedged_service() -> Generator[None]:
    path = service_socket()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)

    def accept_and_drop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=accept_and_drop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        listener.close()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)


def test_a_host_with_no_service_has_no_sessions() -> None:
    rows = collect_sessions(
        service_backend=PosixProcSessionBackend(), shell_backend=get_shell_backend(), now=_now()
    )

    assert rows == ()
    assert agent_shell_sessions(7, now=_now()) == []


def test_the_operations_on_a_shell_of_a_host_with_no_service_find_nothing() -> None:
    assert kill_shell(7, 3) == (ShellKillMode.ABSENT, False, None)
    assert kill_agent_shells(7, before_session_index=10) == []
    with pytest.raises(ShellNotFoundError):
        capture_shell(7, 3, now=_now())


# meiva:note
# An operation keyed on "is this shell there" must not read a service that did not answer as
# "no shells": the TTL reaper deletes a shell's deadline on an `absent` verdict, so a wedged
# service would have a live shell forgotten. The operation fails and is retried instead.
@pytest.mark.parametrize(
    "operation",
    [
        lambda: agent_shell_sessions(7, now=_now()),
        lambda: kill_shell(7, 3),
        lambda: kill_agent_shells(7, before_session_index=10),
        lambda: capture_shell(7, 3, now=_now()),
    ],
    ids=["probe", "kill", "kill-all", "capture"],
)
def test_a_service_that_does_not_answer_fails_the_shell_operations(
    operation: Callable[[], object],
) -> None:
    with _wedged_service(), pytest.raises(client.ServiceUnavailableError):
        operation()


def test_a_service_that_does_not_answer_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _wedged_service(), caplog.at_level(logging.WARNING, logger="ops.cluster_status.sessions"):
        rows = collect_sessions(
            service_backend=PosixProcSessionBackend(),
            shell_backend=get_shell_backend(),
            now=_now(),
        )

    assert rows == ()
    assert [r.getMessage() for r in caplog.records] == [
        "session enumeration failed on PtySessionBackend"
    ]
