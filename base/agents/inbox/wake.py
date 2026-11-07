from __future__ import annotations

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from base.cluster import inbound_channel
from base.db.protocols import DatabaseLike
from base.events.live.bus import EventBus
from base.log import logger

__all__ = ["publish_inbound_wake"]


# meiva:note
# Best-effort Redis publish to wake an idle agent — the fast path paired with the
# claim loop's SELECT recheck. Pub/sub is fire-and-forget: a wake published while the
# agent's listener is disconnected is not replayed, and the agent picks the inbound
# up on its next SELECT recheck.
def publish_inbound_wake(db: DatabaseLike, bus: EventBus, agent_id: int, payload: str) -> bool:
    _ = db
    channel = inbound_channel(agent_id, bus.channel)
    try:
        r = bus.sync_redis()
        try:
            r.publish(channel, payload)  # pyright: ignore[reportUnknownMemberType]
            return True
        finally:
            r.close()
    except ResponseError as exc:
        logger.warning(
            "inbound wake publish to {ch!r} rejected by redis ({exc!r}) — the "
            "cluster redis ACL user lacks this channel; instant wake off, agents "
            "fall back to their SELECT recheck. Check ensure_cluster_redis_acl.",
            ch=channel,
            exc=exc,
        )
        return False
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
        logger.debug(
            "inbound wake publish to {ch!r} skipped ({exc!r}) — best-effort; the "
            "agent's SELECT recheck delivers within timeout_s.",
            ch=channel,
            exc=exc,
        )
        return False
