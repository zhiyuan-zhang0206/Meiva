from __future__ import annotations

from typing import Any

from redis import Redis


# meiva:note
# redis-py types `pubsub`, `acl_setuser` and `acl_deluser` with Unknown keyword arguments, so
# the subscriber and the ACL user are reached through an Any view of the real client.
def loose(client: Redis) -> Any:
    return client
