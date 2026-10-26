"""Redis pub/sub -> Server-Sent Events bridge.

Two streaming modes:

1. ``event_stream`` — per-agent or broadcast filtered stream.
   Browser ``new EventSource("/api/agents/42/system")`` -> this module filters
   each Redis pubsub event by agent_id and sends in SSE format
   (``data: {json}\n\n``). Native streaming: every ``code_delta`` is
   forwarded the moment it arrives, no buffering / batching.

2. ``throttled_event_stream`` — batched, optionally agent-filtered all-events
   stream. With no filter it pushes every event for every agent; with a filter
   it keeps the selected agents plus system-level ``agent_id == 0`` events.
   There is no role filter. Redis messages are drained in batches and flushed
   at a fixed cadence (default 10/sec). Each flush sends a JSON array of event
   raw payloads as a single SSE data frame: ``data: [{...}, ...]\n\n``.

Wire format for ``event_stream``: the SSE ``data:`` line is directly the
``model_dump_json()`` of an ``events.Event``. The frontend's ``JSON.parse``
gets the same ``{role, agent_id, content, ...}`` shape.

``event_stream`` accepts optional ``channel``, ``role_filter``, and
``broadcast`` parameters. ``broadcast=True`` disables agent_id filtering.
"""

import logging
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any

import redis
from fastapi import Request
from pydantic import TypeAdapter, ValidationError
from redis.asyncio.client import PubSub
from redis.exceptions import AuthenticationError, NoPermissionError

from base.events.live.bus import EventBus
from base.events.live.projection import EVENT_ADAPTER, Error, Event
from base.events.live.redis_client import AuthRetryAsyncRedis, retry_auth_failures_async
from base.events.live.redis_resilience import AuthRetry
from gateway.events.sse_frames import (
    HEARTBEAT_FRAME,
    SseCadence,
    sse_batch_frame,
    sse_frame,
    warm_frame,
)
from gateway.http.middleware import runtime_metrics, stopping

_log = logging.getLogger(__name__)

# Exceptions Redis IO may raise — pubsub connection raises
# ConnectionError / TimeoutError on Redis restart / network jitter;
# OSError as a fallback for the socket layer (BrokenPipe etc.).
# TypeError: redis-py writes a connection whose asyncio transport already fired
# connection_lost (`TypeError: 'NoneType' object is not callable`, agent-2613
# crash 2026-08-04 — see base/redis_client._TransportAwareAsyncConnection).
# The SSE stream is a per-request short-lived connection, so treat it as any
# other IO failure: emit an error frame and let the frontend reconnect.
REDIS_IO_ERRORS = (redis.ConnectionError, redis.TimeoutError, OSError, TypeError)
REDIS_ACL_ERRORS = (AuthenticationError, NoPermissionError)


def _error_payload(agent_id: int, exc: Exception) -> str:
    return Error(
        agent_id=agent_id,
        content=f"event stream interrupted: {type(exc).__name__}",
    ).model_dump_json()


def _matches_filter(
    event: Event, agent_id: int, *, broadcast: bool, role_filter: frozenset[str] | None
) -> bool:
    return (broadcast or event.agent_id == agent_id) and (
        not role_filter or event.role in role_filter
    )


async def _subscribe_error_frame(
    pubsub: PubSub, channel: str, agent_id: int, *, auth_retry: AuthRetry, batched: bool = False
) -> bytes | None:
    """Return a wire-shaped error after a failed subscribe, else None."""
    try:
        await retry_auth_failures_async(lambda: pubsub.subscribe(channel), auth_retry=auth_retry)
    except REDIS_IO_ERRORS + REDIS_ACL_ERRORS as exc:
        _log.warning("sse pubsub subscribe failed: %r agent_id=%s", exc, agent_id)
        payload = _error_payload(agent_id, exc)
        return sse_batch_frame([payload]) if batched else sse_frame(payload)
    return None


# meiva:note
# Poll one non-subscribe message from the Redis pubsub handle with a bounded timeout.
# meiva:link gateway/events/docs/sse/sse.meiva.okf.md
async def get_pubsub_message(pubsub: PubSub, timeout: float) -> Mapping[str, object] | None:
    return await pubsub.get_message(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        ignore_subscribe_messages=True,
        timeout=timeout,
    )


# meiva:note
# Each cleanup step is independently suppressed + logged so one failure does not
# swallow another; `channels` accepts the whole subscription set of one pubsub
# connection, which the merged stream subscribes to both of its sources on.
# meiva:link gateway/events/docs/sse/sse.meiva.okf.md
async def close_pubsub(
    pubsub: PubSub, client: AuthRetryAsyncRedis, channels: str | Sequence[str]
) -> None:
    names = (channels,) if isinstance(channels, str) else tuple(channels)

    async def _unsubscribe() -> None:
        # meiva:note
        # Unsubscribe all channels on the pubsub socket before closing it.
        # meiva:link gateway/events/docs/sse/sse.meiva.okf.md
        await pubsub.unsubscribe(*names)  # pyright: ignore[reportUnknownMemberType]

    for step, coro_fn in (
        ("unsubscribe", _unsubscribe),
        ("pubsub.aclose", pubsub.aclose),
        ("client.aclose", client.aclose),
    ):
        try:
            await coro_fn()
        except Exception as exc:
            _log.warning("sse cleanup %s failed: %r", step, exc)


# meiva:note
# Yield a stream of SSE frames (bytes) until the client disconnects or Redis goes down.
#
# Lifetime defenses:
# - `await pubsub.subscribe(...)` runs before the first yield, but StreamingResponse has
#   already sent HTTP 200 by then. If Redis rejects the subscription, emit an `error` data
#   frame and close the stream. The surrounding try/finally still cleans up the pubsub
#   connection.
# - The `yield` is inside the try: when a client aborts on the first frame, `GeneratorExit`
#   is raised; finally must run, otherwise the Redis pubsub socket leaks and a long run hits
#   maxclients.
# - In the `while` loop, when `get_message` / `is_disconnected` raise (Redis restart / ASGI
#   receive exception): publish a single `error` event frame to the client and return; the
#   frontend sees a toast instead of just console noise.
# - In finally, each cleanup step is independently suppressed + logged so one failure does
#   not swallow another.
#
# Args:
#   bus: the cluster event bus. Each connection gets its own pubsub socket, so FastAPI
#     concurrent requests are naturally isolated.
#   agent_id: only forward events where `event.agent_id == agent_id` (ignored in broadcast
#     mode).
#   request: Starlette `Request`; use `is_disconnected()` for quick exit (otherwise pubsub
#     blocking holds until Redis heartbeat timeout).
#   channel: Redis channel to subscribe to (default the bus's events channel).
#   role_filter: only forward events whose role is in this set; None or empty means no filter.
#   broadcast: True means do not filter by agent_id, forward all.
#   poll_s: the client-disconnect polling window of one Redis read
#     (`gateway.sse_disconnect_poll_seconds`).
#   cadence: when a quiet stream still speaks (heartbeat, keep-alive comment).
#   validator: the pydantic validator each frame must parse as (default EVENT_ADAPTER). A
#     non-agent-events channel (e.g. the alerts stream) passes its own TypeAdapter so frames
#     are validated against the channel's shape instead of being dropped.
#
# Yields:
#   bytes: SSE frame (`data: ...\n\n`).
async def event_stream(
    bus: EventBus,
    agent_id: int,  # ignored in broadcast mode
    request: Request,
    *,
    channel: str | None = None,
    role_filter: frozenset[str] | None = None,
    broadcast: bool = False,
    validator: TypeAdapter[Any] = EVENT_ADAPTER,
    poll_s: float,
    cadence: SseCadence,
) -> AsyncGenerator[bytes, None]:
    _channel = channel if channel is not None else bus.channel
    client = bus.open_async_redis()
    pubsub: Any = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
    # Attach/detach bracket: a reconnect storm (client watchdog cycling) or a
    # subscriber that never detaches is invisible without the pair; the detach
    # line carries duration + frames so a half-open stream is distinguishable
    # from a healthy long-lived one.
    opened = time.monotonic()
    data_frames = 0
    metrics_opened = False
    _log.info("sse attach: agent_id=%s channel=%s broadcast=%s", agent_id, _channel, broadcast)
    try:
        if error_frame := await _subscribe_error_frame(
            pubsub, _channel, agent_id, auth_retry=bus.auth_retry
        ):
            yield error_frame
            return
        runtime_metrics.sse_opened("filtered")
        metrics_opened = True

        yield b": stream open\n\n"

        # Last time a real `data:` frame went out (business event or heartbeat).
        # The `: hb` comment does NOT count — the browser can't see it, so it
        # can't keep the client watchdog alive.
        last_data_frame = time.monotonic()
        last_comment = time.monotonic()

        while True:
            if stopping.is_stopping():
                return  # a clean end: the client's EventSource reconnects to the next gateway
            try:
                if await request.is_disconnected():
                    return
                msg = await retry_auth_failures_async(
                    lambda: pubsub.get_message(
                        ignore_subscribe_messages=True,
                        # Client disconnect-detection polling window; deltas shorter
                        # than this are negligible compared to network RTT. env
                        # override: `MEIVA_SSE_DISCONNECT_POLL_SECONDS`.
                        timeout=poll_s,
                    ),
                    auth_retry=bus.auth_retry,
                )
            except REDIS_IO_ERRORS + REDIS_ACL_ERRORS as exc:
                _log.warning("sse pubsub read failed: %r agent_id=%s", exc, agent_id)
                yield sse_frame(_error_payload(agent_id, exc))
                return

            now = time.monotonic()
            if msg is not None and isinstance(msg["data"], str):
                try:
                    event = validator.validate_json(msg["data"])
                except ValidationError:
                    yield f": dropped unparseable payload ({len(msg['data'])} bytes)\n\n".encode()
                    last_comment = now
                    continue
                # broadcast mode: do not filter by agent_id
                if _matches_filter(event, agent_id, broadcast=broadcast, role_filter=role_filter):
                    yield sse_frame(msg["data"])
                    data_frames += 1
                    last_data_frame = now
                    last_comment = now
                    continue

            # Nothing business-relevant this tick (quiet channel, a filtered
            # event, a non-str or unparseable payload): keep the wire warm —
            # meiva:note
            # heartbeat once the silence crosses cadence.heartbeat_s, otherwise
            # a throttled keep-alive comment.
            frame, last_comment, last_data_frame = warm_frame(
                now, last_comment, last_data_frame, cadence
            )
            if frame is not None:
                yield frame
    finally:
        if metrics_opened:
            runtime_metrics.sse_closed("filtered")
        _log.info(
            "sse detach: agent_id=%s duration=%.1fs data_frames=%d",
            agent_id,
            time.monotonic() - opened,
            data_frames,
        )
        await close_pubsub(pubsub, client, _channel)


async def _drain_redis_messages(
    pubsub: PubSub,
    request: Request,
    flush_interval: float,
    *,
    agent_filter: set[int] | None,
    auth_retry: AuthRetry,
) -> tuple[list[str], bool]:
    """Drain all available messages from Redis within one flush window.

    Returns (buffer, disconnected). If disconnected is True, the caller
    should stop the stream.

    The client-disconnect check runs once per drain cycle (not on every
    message) — during a burst of hundreds of chat_delta / code_delta /
    exec_output_chunk events the inner loop can iterate fast, and
    ``request.is_disconnected()`` (``await self._receive()`` under uvicorn)
    adds non-trivial per-iteration overhead that stacks into seconds of
    cumulative lag when called on every event.
    """
    # Check disconnect once per drain cycle — a disconnect detected on
    # the next cycle (≤ flush_interval away) is a rounding error. A gateway
    # shutdown ends the stream the same way.
    if stopping.is_stopping() or await request.is_disconnected():
        return [], True

    buffer: list[str] = []
    drain_deadline = time.monotonic() + flush_interval

    while time.monotonic() < drain_deadline:
        remaining = drain_deadline - time.monotonic()
        if remaining <= 0:
            break
        poll = min(remaining, 0.1)
        msg = await retry_auth_failures_async(
            lambda poll=poll: get_pubsub_message(pubsub, poll), auth_retry=auth_retry
        )
        if msg is None:
            break
        raw = msg["data"]
        if not isinstance(raw, str):
            continue
        try:
            event = EVENT_ADAPTER.validate_json(raw)
        except ValidationError:
            continue
        if agent_filter is not None and event.agent_id != 0 and event.agent_id not in agent_filter:
            continue
        buffer.append(raw)

    return buffer, False


# meiva:note
# Yield a throttled, batched SSE stream of events.
#
# With no `agent_filter`, every agent's events pass. With a filter, selected agents and
# system-level `agent_id == 0` events pass. There is no role filter. Events are drained from
# Redis in batches and flushed at a fixed cadence (`1 / throttle_rate` seconds). Each flush
# sends a JSON array of raw event payloads as a single SSE data frame:
#
#     data: [{...}, {...}, ...]\n\n
#
# Lifetime defenses mirror `event_stream`: a subscription failure after HTTP 200 sends a
# batch-shaped error frame; GeneratorExit-safe finally, Redis-IO recovery, and client
# disconnect detection also apply.
#
# Args:
#   bus: the cluster event bus.
#   request: Starlette `Request` for disconnect detection.
#   channel: Redis channel (default the bus's events channel).
#   throttle_rate: SSE pushes per second, required (the caller reads
#     `gateway.sse_throttle_rate`).
#   cadence: when a quiet stream still speaks.
#   agent_filter: agent ids to forward. None forwards every agent; system-level
#     `agent_id == 0` events always pass.
#
# Yields:
#   bytes: SSE frame (`data: [...]\n\n`).
async def throttled_event_stream(
    bus: EventBus,
    request: Request,
    *,
    channel: str | None = None,
    throttle_rate: float,
    cadence: SseCadence,
    agent_filter: set[int] | None = None,
) -> AsyncGenerator[bytes, None]:
    _channel = channel if channel is not None else bus.channel
    client = bus.open_async_redis()
    pubsub: Any = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
    opened = time.monotonic()
    data_frames = 0
    total_events = 0
    metrics_opened = False
    flush_interval = 1.0 / throttle_rate
    _log.info(
        "sse throttle attach: channel=%s throttle_rate=%.1f/s",
        _channel,
        throttle_rate,
    )
    try:
        if error_frame := await _subscribe_error_frame(
            pubsub, _channel, 0, auth_retry=bus.auth_retry, batched=True
        ):
            yield error_frame
            return
        runtime_metrics.sse_opened("throttled")
        metrics_opened = True

        yield b": stream open\n\n"

        last_flush = time.monotonic()

        while True:
            try:
                buffer, disconnected = await _drain_redis_messages(
                    pubsub,
                    request,
                    flush_interval,
                    agent_filter=agent_filter,
                    auth_retry=bus.auth_retry,
                )
            except REDIS_IO_ERRORS + REDIS_ACL_ERRORS as exc:
                _log.warning("sse throttle pubsub read failed: %r", exc)
                yield sse_batch_frame([_error_payload(0, exc)])
                return

            if disconnected:
                return

            now = time.monotonic()
            if buffer:
                yield sse_batch_frame(buffer)
                data_frames += 1
                total_events += len(buffer)
                last_flush = now
            elif now - last_flush >= cadence.heartbeat_s:
                yield HEARTBEAT_FRAME
                last_flush = now
            else:
                yield b": hb\n\n"
    finally:
        if metrics_opened:
            runtime_metrics.sse_closed("throttled")
        _log.info(
            "sse throttle detach: duration=%.1fs data_frames=%d total_events=%d",
            time.monotonic() - opened,
            data_frames,
            total_events,
        )
        for step, coro_fn in (
            ("unsubscribe", lambda: pubsub.unsubscribe(_channel)),  # pyright: ignore[reportUnknownMemberType]
            ("pubsub.aclose", pubsub.aclose),
            ("client.aclose", client.aclose),
        ):
            try:
                await coro_fn()
            except Exception as exc:
                _log.warning("sse throttle cleanup %s failed: %r", step, exc)


__all__ = [
    "REDIS_ACL_ERRORS",
    "REDIS_IO_ERRORS",
    "close_pubsub",
    "event_stream",
    "throttled_event_stream",
]
