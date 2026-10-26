from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from base.sessions.pty import client, protocol


@pytest.mark.parametrize(
    ("wait_s", "attempts"),
    [(0.0, 1), (0.1, 2), (0.25, 4), (5.0, 51)],
)
def test_a_wait_is_one_attempt_plus_one_per_retry_pause_that_fits(
    wait_s: float, attempts: int
) -> None:
    assert client.connect_attempts(wait_s) == attempts


@pytest.mark.parametrize("wait_s", [-0.1, float("nan"), float("inf")])
def test_an_unusable_wait_is_refused(wait_s: float) -> None:
    with pytest.raises(ValueError, match="connect wait"):
        client.connect_attempts(wait_s)


def test_a_zero_wait_gives_up_at_the_first_refusal(short_tmp: Path) -> None:
    with pytest.raises(client.ServiceDownError):
        client.request("ping", socket_path=short_tmp / "service.sock")


def test_a_wait_pauses_between_attempts_before_giving_up(short_tmp: Path) -> None:
    started = time.monotonic()
    with pytest.raises(client.ServiceDownError):
        client.request("ping", socket_path=short_tmp / "service.sock", wait=0.2)
    assert time.monotonic() - started >= 0.2


def test_a_service_that_comes_up_inside_the_wait_answers(short_tmp: Path) -> None:
    ready = threading.Event()
    served: list[str] = []

    def serve() -> None:
        time.sleep(0.3)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.settimeout(10.0)
            listener.bind(str(short_tmp / "service.sock"))
            listener.listen(1)
            ready.set()
            conn, _ = listener.accept()
            with conn:
                line = protocol.read_line(conn, limit=1 << 20)
                assert line is not None
                served.append(protocol.decode_object(line)["method"])
                conn.sendall(protocol.encode({"id": 1, "ok": True, "data": {"up": True}}))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        data = client.request("ping", socket_path=short_tmp / "service.sock", wait=5.0)
    finally:
        thread.join(timeout=10)
    assert ready.is_set()
    assert data == {"up": True}
    assert served == ["ping"]


@pytest.mark.parametrize("wait_s", [-0.1, float("nan"), float("inf")])
def test_a_config_with_an_unusable_wait_is_refused_when_built(
    short_tmp: Path, wait_s: float
) -> None:
    with pytest.raises(ValueError, match="connect wait"):
        client.PtyClientConfig(socket_path=short_tmp / "service.sock", connect_wait_s=wait_s)
