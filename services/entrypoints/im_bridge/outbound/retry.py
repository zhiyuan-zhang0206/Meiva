from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from services.entrypoints.im_bridge.config import ImBridgeConfig
from services.entrypoints.im_bridge.outbound.push_plan import retry_delay_s

__all__ = ["retry_backoff_seconds", "retry_once_after_backoff"]


# meiva:note
# The bounded jitter backoff before a retry, drawn once: `push_plan.retry_delay_s` decides
# the value from the configured base and jitter. The draw spreads retries and has no security
# role.
def retry_backoff_seconds(config: ImBridgeConfig) -> float:
    return retry_delay_s(
        base_s=config.im_push_retry_backoff_seconds,
        jitter_s=config.im_push_retry_jitter_seconds,
        draw=random.random(),  # noqa: S311
    )


# meiva:note
# Sleep a bounded jitter backoff, then run one retry attempt.
#
# Exactly one retry, never a loop. A failure of the retry propagates to the caller, which owns
# the logging and the outcome. Shared by the push path (`send_with_retry`), the outbound worker
# and the ops-alert fan-out (`IMBridgeCore.notify_user`).
async def retry_once_after_backoff(
    attempt: Callable[[], Awaitable[None]], config: ImBridgeConfig
) -> None:
    await asyncio.sleep(retry_backoff_seconds(config))
    await attempt()
