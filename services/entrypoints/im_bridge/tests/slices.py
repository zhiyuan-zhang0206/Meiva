from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import replace
from functools import cache
from typing import Any

from base.config.tests.configuration import snapshot_process_config
from services.entrypoints.im_bridge import daemon
from services.entrypoints.im_bridge.config import (
    ImBridgeConfig,
)
from services.entrypoints.im_bridge.gateway_client import GatewayClient

__all__ = [
    "gateway_client",
    "im_bridge_config",
    "seeded_rng",
    "system_rng",
]


@cache
def _base_im_bridge_config() -> ImBridgeConfig:
    return daemon.im_bridge_config(snapshot_process_config())


# meiva:note
# The bridge's configuration slice as the process configuration describes it, except that a
# retried send waits no time: a test that exercises the retry would otherwise sit through the
# production backoff. A test that cares about a value passes it.
def im_bridge_config(**overrides: Any) -> ImBridgeConfig:
    instant = {"im_push_retry_backoff_seconds": 0.0, "im_push_retry_jitter_seconds": 0.0}
    return replace(_base_im_bridge_config(), **{**instant, **overrides})


def gateway_client(
    config: ImBridgeConfig | None = None, auth_headers: Mapping[str, str] | None = None
) -> GatewayClient:
    return GatewayClient(
        config or im_bridge_config(),
        gateway_url="http://localhost:8000",
        auth_headers=auth_headers or {},
        rng=system_rng(),
    )


# meiva:note
# A generator with a fixed seed, so a test that draws from it sees the same values every run.
def seeded_rng(seed: int = 0) -> random.Random:
    return random.Random(seed)  # noqa: S311


# meiva:note
# The operating system's generator, as the daemon builds it: a test that does not need the same
# draws on every run passes this, so two objects built in one test never mint the same id.
def system_rng() -> random.Random:
    return random.SystemRandom()
