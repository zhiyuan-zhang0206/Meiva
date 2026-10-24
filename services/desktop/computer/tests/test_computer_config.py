from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from base.config import get_field, settings
from services.desktop.computer import mcp_daemon
from services.desktop.computer.config import beat_interval_s, effective_stall_s, loop_is_silent


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "computer_use_lease_s", 7.5)
    monkeypatch.setattr(settings.daemon, "computer_use_session_idle_s", 3.25)
    monkeypatch.setattr(settings.daemon, "computer_use_loop_stall_s", 42.0)
    monkeypatch.setattr(settings.daemon, "computer_use_shutdown_drain_s", 1.5)
    config = mcp_daemon.computer_use_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert (
        config.computer_use_lease_s,
        config.computer_use_session_idle_s,
        config.computer_use_loop_stall_s,
        config.computer_use_shutdown_drain_s,
    ) == (7.5, 3.25, 42.0, 1.5)


@pytest.mark.parametrize(("configured", "used"), [(0.05, 1.0), (1.0, 1.0), (45.0, 45.0)])
def test_a_tiny_window_is_floored(configured: float, used: float) -> None:
    assert effective_stall_s(configured) == used


@pytest.mark.parametrize(
    ("beat_s", "stall_s", "interval"), [(15.0, 45.0, 15.0), (15.0, 6.0, 2.0), (0.01, 1.0, 0.01)]
)
def test_the_rebeat_stays_inside_a_third_of_the_window(
    beat_s: float, stall_s: float, interval: float
) -> None:
    assert beat_interval_s(beat_s, stall_s) == pytest.approx(interval)


@pytest.mark.parametrize(
    ("now", "last_beat", "silent"), [(10.0, 10.0, False), (11.0, 10.0, False), (11.1, 10.0, True)]
)
def test_the_loop_is_silent_only_past_the_window(
    now: float, last_beat: float, silent: bool
) -> None:
    assert loop_is_silent(now=now, last_beat=last_beat, stall_s=1.0) is silent
