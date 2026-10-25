from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base.events.live import bus as bus_module
from base.events.live.bus import EventBus


def patch_async_redis(monkeypatch: pytest.MonkeyPatch, client: Callable[[], Any]) -> None:

    def async_redis(_bus: EventBus) -> Any:
        return client()

    monkeypatch.setattr(EventBus, "async_redis", async_redis)


def patch_open_async_redis(
    monkeypatch: pytest.MonkeyPatch, open_client: Callable[..., Any]
) -> None:
    monkeypatch.setattr(bus_module, "open_async_redis", open_client)


def patch_sync_redis(monkeypatch: pytest.MonkeyPatch, client: Callable[[], Any]) -> None:

    def sync_redis(_bus: EventBus, *, decode_responses: bool = False) -> Any:
        del decode_responses
        return client()

    monkeypatch.setattr(EventBus, "sync_redis", sync_redis)
