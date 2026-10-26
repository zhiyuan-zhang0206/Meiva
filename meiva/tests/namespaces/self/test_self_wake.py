from __future__ import annotations

import secrets
import socket
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from base.events.live.redis_client import open_sync_redis
from base.events.live.tests.rng import seeded_auth_retry
from base.log.tests.log_records import LogRecords
from meiva.self.wake import publish_inbound_wake
from meiva.tests.namespaces.self.redis_view import loose

_EVENTS_CHANNEL = "wakecase:events"
_INBOUND_41 = "wakecase:inbound:41"


@pytest.fixture
def client(redis_url: str) -> Iterator[Redis]:
    connection = open_sync_redis(redis_url, decode_responses=True, auth_retry=seeded_auth_retry())
    try:
        yield connection
    finally:
        connection.close()


def test_the_wake_reaches_a_subscriber_of_the_agents_own_inbound_channel(client: Redis) -> None:
    subscription = loose(client).pubsub()
    subscription.subscribe(_INBOUND_41)
    assert subscription.get_message(timeout=5)["type"] == "subscribe"

    publish_inbound_wake(client, 41, events_channel=_EVENTS_CHANNEL)

    message = subscription.get_message(timeout=5)
    subscription.close()
    assert message is not None
    assert (message["channel"], message["data"]) == (_INBOUND_41, "0")


def test_the_wake_does_not_reach_another_agents_channel(client: Redis) -> None:
    other = loose(client).pubsub()
    other.subscribe("wakecase:inbound:42")
    assert other.get_message(timeout=5)["type"] == "subscribe"

    publish_inbound_wake(client, 41, events_channel=_EVENTS_CHANNEL)

    assert other.get_message(timeout=0.3) is None
    other.close()


def test_a_redis_acl_refusal_is_logged_and_never_raised(
    client: Redis, redis_url: str, loguru_records: LogRecords
) -> None:
    token = secrets.token_hex(8)
    loose(client).acl_setuser(
        "wakeless",
        enabled=True,
        passwords=[f"+{token}"],
        keys=["*"],
        commands=["+publish"],
        channels=["somewhere-else:*"],
    )
    address = urlsplit(redis_url)
    restricted = Redis(
        host=address.hostname or "127.0.0.1",
        port=address.port or 6379,
        username="wakeless",
        password=token,
    )
    try:
        publish_inbound_wake(restricted, 41, events_channel=_EVENTS_CHANNEL)
    finally:
        restricted.close()
        loose(client).acl_deluser("wakeless")

    assert any("rejected by redis" in text for text in loguru_records.messages("WARNING"))


@pytest.mark.usefixtures("redis_url")
def test_an_unreachable_redis_is_logged_and_never_raised(loguru_records: LogRecords) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    unreachable = Redis(
        host="127.0.0.1",
        port=closed_port,
        socket_connect_timeout=1,
        retry=Retry(NoBackoff(), 0),
    )
    try:
        publish_inbound_wake(unreachable, 41, events_channel=_EVENTS_CHANNEL)
    finally:
        unreachable.close()

    assert any("failed; the wake falls back" in text for text in loguru_records.messages("WARNING"))
