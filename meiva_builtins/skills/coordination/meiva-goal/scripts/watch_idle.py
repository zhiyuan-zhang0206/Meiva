# meiva:note
# Reference watcher body for the meiva-goal skill.
#
# After substituting TARGET_AGENT_ID, write this code to a script file and launch
# it with `meiva.shell.run_background(command, ttl=21600, name="goal-idle")`. It waits until the agent you are watching finishes
# a turn and goes idle, then delivers one wake message (`meiva.agents.send_message`) so you wake
# up and judge that agent's progress against the goal.
#
# The wake is sent once; the SDK's own bounded retries are the only retry. If
# the send still fails the watcher logs the error and exits 2, so the launcher
# gets an exit notice and re-launches; a wake that lands in a gateway / agent
# restart window is not redelivered.
#
# Runtime notes:
# - This program runs under the same Python environment, configuration, and
#   machine credentials as the agent that launched it.
# - `meiva.agents.send_message(meiva.self.AGENT_ID, content)` delivers `content` back to you (the launching agent)
#   as a new incoming message.
# - Connection settings are read from the launching agent's own configuration, so
#   the watcher always listens on the same event stream the watched agent reports
#   to.
# - `socket_timeout=None` is mandatory: redis-py 8 defaults it to 5s, which kills
#   pubsub.listen()'s long blocking read the moment the event stream goes quiet
#   for a few seconds (observed 2026-08-13: the watcher died mid-watch and would
#   have missed the target's idle). If the stream still dies (redis restart,
#   connection loss), the watcher falls back to polling the target's status so
#   the goal loop never stalls.

import json
import time
from collections.abc import Iterator
from contextlib import closing
from typing import Protocol, Self, TypeGuard

import redis

import meiva
from base.config import settings
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

# meiva:note
# Substitute before launching: the id of the agent you want to watch.
TARGET_AGENT_ID = 0

# meiva:note
# An agent reports a lifecycle update every time its status changes. "idling"
# means it ended its turn and is waiting for the next message -- the moment to
# check whether the goal is met.
IDLE_STATUS = "idling"


class _PubSub(Protocol):
    def subscribe(self, channel: str) -> None: ...

    def listen(self) -> Iterator[dict[str, object]]: ...

    def close(self) -> None: ...


class _RedisClient(Protocol):
    @classmethod
    def from_url(
        cls, url: str, *, decode_responses: bool, socket_timeout: float | None
    ) -> Self: ...

    def pubsub(self) -> _PubSub: ...


def _is_redis_client_cls(value: object) -> TypeGuard[type[_RedisClient]]:
    return isinstance(value, type)


def _is_target_idle(event: dict[str, object], target_id: int) -> bool:
    if event.get("role") != "agent_updated" or event["agent_id"] != target_id:
        return False
    return meiva.agents.get_status(target_id) == IDLE_STATUS


def _notify(target_id: int) -> None:
    message = (
        f"target agent {target_id} idled -- inspect its recent output and judge it against the goal"
    )
    try:
        meiva.agents.send_message(meiva.self.AGENT_ID, message)
    except Exception as exc:
        print(f"wake delivery failed: {exc!r}", flush=True)
        raise SystemExit(2) from None


def _watch_via_poll(target_id: int, interval_s: float = 5.0) -> None:
    # meiva:note
    # Fallback: poll the target's status until it idles, then remind once.
    #
    #     Used when the event stream is unavailable, when the target may ALREADY be
    #     idle before the watcher starts, or when a pubsub read died mid-watch. It
    #     reads the same authoritative status as the event path. A transient read
    #     error only skips one round; the 6h background shell TTL remains the
    #     outer safety bound, so a persistently failing read also wakes the launching
    #     agent eventually (via the watcher's own exit).
    #
    failing = False
    while True:
        try:
            idle = meiva.agents.get_status(target_id) == IDLE_STATUS
        except Exception as exc:
            if not failing:
                failing = True
                print(f"status read failed, retrying every {interval_s}s: {exc!r}", flush=True)
            time.sleep(interval_s)
            continue
        if failing:
            failing = False
            print("status read recovered", flush=True)
        if idle:
            _notify(target_id)
            return
        time.sleep(interval_s)


# meiva:note
# Subscribe before reading status so an already-idle target wakes the launcher immediately.
# The watch exits after one reminder; a later idle transition needs a fresh launch.
def watch(target_id: int, *, redis_cls: object = redis.Redis) -> None:
    if not _is_redis_client_cls(redis_cls):
        raise TypeError("redis.Redis is not a class")
    try:
        client = redis_cls.from_url(
            settings.data_plane.redis_url,
            decode_responses=True,
            # meiva:note
            # A quiet subscription must survive beyond redis-py's default timeout.
            socket_timeout=None,
        )
        with closing(client.pubsub()) as pubsub:
            pubsub.subscribe(settings.data_plane.events_channel)
            if meiva.agents.get_status(target_id) != IDLE_STATUS:
                for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    payload = message["data"]
                    if not isinstance(payload, str):
                        continue
                    event = object_of(json.loads, payload)
                    if not is_object_dict(event):
                        continue
                    if _is_target_idle(event, target_id):
                        break
                else:
                    return
    except (
        redis.TimeoutError,
        redis.ConnectionError,
        TimeoutError,
        ConnectionError,
        OSError,
        meiva.agents.GatewayUnavailable,
    ):
        # meiva:note
        # The stream could not connect or died mid-watch. The
        # target may idle -- or already have -- while we are blind: poll the
        # table so the launching agent does not stall silently.
        _watch_via_poll(target_id)
        return
    _notify(target_id)


if __name__ == "__main__":
    watch(TARGET_AGENT_ID)
