from collections.abc import Mapping
from typing import Protocol

from redis.asyncio.client import PubSub


# meiva:note
# The one async pubsub call the agent tests make, typed here because redis-py leaves it partly
# Unknown.
class AsyncMessageSource(Protocol):
    async def get_message(
        self, *, ignore_subscribe_messages: bool = False, timeout: float
    ) -> Mapping[str, object] | None: ...


def async_messages(pubsub: PubSub) -> AsyncMessageSource:
    return pubsub
