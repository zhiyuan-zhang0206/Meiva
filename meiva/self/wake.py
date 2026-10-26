# meiva:note
# The best-effort Redis wake `meiva.self` sends after inserting its own inbound row.
#
# The namespace module `meiva/self/__init__.py` is the facade: it reads the bound process
# context for the redis handle, the agent's identity and the configured events channel, then
# calls this function with all three, so a test passes a redis handle, an id and a channel
# name instead of replacing anything.

from __future__ import annotations

from typing import Protocol

from loguru import logger
from redis.exceptions import ResponseError

from base.cluster import inbound_channel

__all__ = ["publish_inbound_wake"]


class _RedisPublisher(Protocol):
    def publish(self, channel: str, message: str) -> object: ...


class _LazyRedis(Protocol):
    def __getattr__(self, attr: str) -> object: ...


# meiva:note
# Publish the wake for `agent_id`'s own inbound channel; never raises. `redis` is the SDK
# redis handle (anything with `publish(channel, message)`) and `events_channel` the cluster's
# configured events channel the inbound channel derives from.
#
# Same contract as `base.agents.impersonation.wake.publish_inbound_wake`: the claim loop's
# SELECT recheck delivers the row regardless, but a `ResponseError` (a `NoPermissionError`
# is one) is logged as an ACL misconfiguration rather than swallowed - this agent's redis
# ACL user is not granted its own `<prefix>:inbound:*` channel. The channel comes from
# `inbound_channel`, so it matches `RedisInboundListener` and stays inside the ACL grant.
# meiva:link meiva/self/docs/self.meiva.okf.md
def publish_inbound_wake(
    redis: _RedisPublisher | _LazyRedis, agent_id: int, *, events_channel: str
) -> None:
    channel = inbound_channel(agent_id, events_channel)
    try:
        redis.publish(channel, "0")  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportCallIssue]
    except ResponseError as exc:
        logger.warning(
            "self inbound wake publish to {ch!r} rejected by redis ({exc!r}) - "
            "this agent's redis ACL user lacks its inbound channel; the "
            "self-restart/terminate/compact wake falls back to the claim SELECT "
            "recheck. Check ensure_cluster_redis_acl.",
            ch=channel,
            exc=exc,
        )
    except Exception:
        logger.opt(exception=True).warning(
            "self inbound wake publish to {ch!r} failed; the wake falls back to the claim "
            "SELECT recheck",
            ch=channel,
        )
