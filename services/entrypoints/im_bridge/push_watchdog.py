"""Weixin push-failure watchdog (Task #829).

iLink's context_token (carried by each user message) expires after a short,
undetermined window (measured 11min..2h+). When it does, sendmessage fails
silently with ret=-2 while getUpdates keeps working — the user can still
message the bot but never receives replies. The only refresh is the user's
own message, so:

- a failed retry emits the ``im_push_failed`` event carrying the adapter's
  consecutive-failure count; the alert rule over the event stream tells the
  user (through the ops fan-out) to message the bot;
- after a later send succeeds (a fresh user message brought a new token),
  hint "recovered" on the next inbound reply.
"""

from __future__ import annotations

import logging
import random
from typing import Protocol

from base.log import logger
from services.entrypoints.im_bridge import copy
from services.entrypoints.im_bridge.config import ImBridgeConfig
from services.entrypoints.im_bridge.outbound.push_plan import recovered_hint_due
from services.entrypoints.im_bridge.outbound.retry import retry_once_after_backoff
from services.entrypoints.im_bridge.types import (
    IMAdapter,
    InboundMessage,
    Reply,
    SendNotStartedError,
    SendOutcomeUncertainError,
)

_log = logging.getLogger("services.entrypoints.im_bridge.core.push_watchdog")


class RetryConfigCore(Protocol):
    config: ImBridgeConfig


class WatchdogAdapter(Protocol):
    push_failures: int

    async def send(
        self,
        chat_id: str,
        text: str,
        /,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None: ...


class WatchdogCore(Protocol):
    config: ImBridgeConfig
    adapters: dict[str, IMAdapter]

    async def _send(
        self, channel: str, chat_id: str, reply: Reply, *, adapter: IMAdapter | None = None
    ) -> None: ...


async def send_with_retry(
    core: RetryConfigCore,
    channel: str,
    chat_id: str,
    reply: Reply,
    adapter: WatchdogAdapter,
    *,
    rng: random.Random,
) -> None:
    """Retry only an adapter-proven unstarted send, never an ambiguous prefix."""

    async def attempt() -> None:
        await adapter.send(chat_id, reply.text, buttons=reply.buttons, markdown=reply.markdown)

    try:
        await attempt()
    except SendNotStartedError as exc:
        # The adapter guarantees no chunk was accepted. Only this explicit
        # boundary proof permits retrying the entire logical message.
        _log.warning("send failed channel=%s chat=%s: %r — retrying once", channel, chat_id, exc)
        try:
            await retry_once_after_backoff(attempt, core.config, rng=rng)
        except (SendNotStartedError, SendOutcomeUncertainError):
            _log.exception("send retry failed channel=%s chat=%s", channel, chat_id)
            logger.warning(
                "push failed after retry: {channel}",
                event="im_push_failed",
                channel=channel,
                failures=adapter.push_failures,
            )
    except SendOutcomeUncertainError:
        _log.exception("send outcome uncertain channel=%s chat=%s; no retry", channel, chat_id)
        logger.warning(
            "push outcome uncertain: {channel}",
            event="im_push_failed",
            channel=channel,
            failures=adapter.push_failures,
        )


async def hint_recovered(core: WatchdogCore, msg: InboundMessage, *, now: float) -> None:
    """Right after a weixin push failure, the user's own message carries a
    fresh context_token and the first send succeeds — tell them the link is
    back."""
    if msg.channel != "weixin":
        return
    adapter = core.adapters.get("weixin")
    if adapter is None:
        return
    if not recovered_hint_due(recovered_at=adapter.push_recovered_at, now=now):
        return

    try:
        await core._send(
            msg.channel,
            msg.chat_id,
            Reply(copy.PUSH_RECOVERED_HINT.format(channel=msg.channel)),
        )
    except (SendNotStartedError, SendOutcomeUncertainError):
        _log.exception("weixin recovered-hint send failed")
