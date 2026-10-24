from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import cache
from typing import Any

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
    )
