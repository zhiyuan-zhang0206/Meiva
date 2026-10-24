# meiva:note
# The Redis relay rebuilds its listener and backs off while its interface is missing; converge
# retires the standalone bridge on gateway hosts.

from __future__ import annotations

from typing import Any, cast

import pytest

from cli.commands.converge import redis_bridge as bridge
from cli.commands.lifecycle import converge_host
from services.redis_bridge import relay


class _StopTestError(RuntimeError):
    pass


class _BrokenListener:
    def __init__(self) -> None:
        self.closed = False
        self.accept_calls = 0

    def accept(self) -> None:
        self.accept_calls += 1
        if self.accept_calls == 1:
            raise OSError("descriptor is closed")
        raise _StopTestError

    def close(self) -> None:
        self.closed = True


class _RecoveredListener:
    def accept(self) -> None:
        raise _StopTestError

    def close(self) -> None:
        return


# meiva:note
# The #5740 failure: accept errors must replace the dead descriptor.
def test_relay_rebuilds_listener_after_accept_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = _BrokenListener()
    listeners = iter([broken, _RecoveredListener()])
    opened: list[tuple[str, int]] = []
    sleeps: list[float] = []

    def _open(address: tuple[str, int]) -> object:
        opened.append(address)
        return next(listeners)

    with pytest.raises(_StopTestError):
        relay.serve_forever(
            ("10.64.0.7", 6380),
            ("127.0.0.1", 6380),
            open_listener=cast("Any", _open),
            sleep=sleeps.append,
        )

    assert opened == [("10.64.0.7", 6380), ("10.64.0.7", 6380)]
    assert broken.closed
    assert sleeps == [relay.INITIAL_REBIND_DELAY_S]
    assert "descriptor is closed; rebuilding listener" in capsys.readouterr().out


def test_listener_bind_failures_back_off_until_interface_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def _open(_address: tuple[str, int]) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise OSError("address not available")
        raise _StopTestError

    with pytest.raises(_StopTestError):
        relay.serve_forever(
            ("10.64.0.7", 6380),
            ("127.0.0.1", 6380),
            open_listener=cast("Any", _open),
            sleep=sleeps.append,
        )

    assert sleeps == [1.0, 2.0, 4.0]


def test_retire_step_runs_on_gateway_hosts() -> None:
    step = next(
        candidate
        for candidate in converge_host.CONVERGE_STEPS
        if candidate.name == "retire the standalone Redis bridge LaunchAgent"
    )

    assert step.apply is bridge.retire_launchd_redis_bridge
    assert step.roles == frozenset({"gateway"})
    assert step.host_global
