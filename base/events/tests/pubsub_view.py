from collections.abc import Mapping
from typing import Protocol

from redis.client import PubSub


# meiva:note
# The one pubsub call the delivery tests make, typed here because redis-py leaves it partly
# Unknown.
class MessageSource(Protocol):
    def get_message(self, *, timeout: float) -> Mapping[str, object] | None: ...


def messages(pubsub: PubSub) -> MessageSource:
    return pubsub
