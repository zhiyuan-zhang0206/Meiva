from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import replace
from functools import cache
from typing import Any
from uuid import uuid4

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.config.tests.configuration import snapshot_process_config
from services.entrypoints.im_bridge import daemon
from services.entrypoints.im_bridge.config import (
    FeishuCredentialsConfig,
    ImBridgeConfig,
    TelegramCredentialsConfig,
)
from services.entrypoints.im_bridge.gateway_client import GatewayClient

__all__ = [
    "feishu_config",
    "gateway_client",
    "im_bridge_config",
    "seeded_rng",
    "system_clock",
    "telegram_config",
]


@cache
def _base_im_bridge_config() -> ImBridgeConfig:
    return daemon.im_bridge_config(snapshot_process_config())


@cache
def _base_telegram_config() -> TelegramCredentialsConfig:
    return daemon.telegram_config(snapshot_process_config())


@cache
def _base_feishu_config() -> FeishuCredentialsConfig:
    return daemon.feishu_config(snapshot_process_config())


# meiva:note
# The bridge's configuration slice as the process configuration describes it, except that a
# retried send waits no time: a test that exercises the retry would otherwise sit through the
# production backoff. A test that cares about a value passes it.
def im_bridge_config(**overrides: Any) -> ImBridgeConfig:
    instant = {"im_push_retry_backoff_seconds": 0.0, "im_push_retry_jitter_seconds": 0.0}
    return replace(_base_im_bridge_config(), **{**instant, **overrides})


def telegram_config(**overrides: Any) -> TelegramCredentialsConfig:
    return replace(_base_telegram_config(), **overrides)


def feishu_config(**overrides: Any) -> FeishuCredentialsConfig:
    return replace(_base_feishu_config(), **overrides)


def gateway_client(
    config: ImBridgeConfig | None = None, auth_headers: Mapping[str, str] | None = None
) -> GatewayClient:
    return GatewayClient(
        config or im_bridge_config(),
        gateway_url="http://localhost:8000",
        auth_headers=auth_headers or {},
        new_id=uuid4,
    )


# meiva:note
# The real wall clock and timers, as the daemon builds them: a test that does not steer time
# passes this, one that does builds a `Clock` over `ManualTimers`.
def system_clock() -> Clock:
    return Clock(ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=SYSTEM_TIMERS)


# meiva:note
# A generator with a fixed seed, so a test that draws from it sees the same values every run.
def seeded_rng(seed: int = 0) -> random.Random:
    return random.Random(seed)  # noqa: S311
