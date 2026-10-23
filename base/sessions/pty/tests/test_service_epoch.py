from __future__ import annotations

from pathlib import Path

from base.paths.tests.homes import unit_home as unit_home
from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import PtyServiceProcess, service_socket
from base.sessions.pty.tests.pty_service import pty_service as pty_service


def test_the_host_generation_names_one_serving_generation(pty_service: PtyServiceProcess) -> None:
    first = client.host_generation(socket_path=service_socket())

    assert first
    assert client.host_generation(socket_path=service_socket()) == first
    assert client.list_sessions(socket_path=service_socket()) == []


def test_a_replaced_service_answers_a_new_generation(pty_service: PtyServiceProcess) -> None:
    first = client.host_generation(socket_path=service_socket())

    pty_service.stop()
    pty_service.start()

    replacement = client.host_generation(socket_path=service_socket())
    assert replacement
    assert replacement != first


def test_an_absent_service_has_no_generation_even_though_its_session_list_is_empty(
    unit_home: Path,
) -> None:
    del unit_home

    assert client.host_generation(socket_path=service_socket()) is None
    assert client.list_sessions(socket_path=service_socket()) == []
