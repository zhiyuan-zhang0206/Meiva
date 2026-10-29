from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

from base.config import get_field, settings
from services.agent_runner.page_server import daemon


def test_the_slice_carries_the_live_value_of_every_field() -> None:
    config = daemon.page_server_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    fake_daemon: Any = SimpleNamespace(
        page_server_poll_interval_seconds=9.5,
        heartbeat_interval_seconds=settings.daemon.heartbeat_interval_seconds,
        page_server_db_failure_exit_seconds=settings.daemon.page_server_db_failure_exit_seconds,
    )
    custom = daemon.page_server_config(daemon_settings=fake_daemon)
    assert custom.page_server_poll_interval_seconds == 9.5
