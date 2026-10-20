# meiva:note
# One SSE connection carries the Web UI's two Redis sources as three named
# channels, so a browser holds one socket instead of one per projection. Every
# channel owns its bounded queue and its own error frame; a shed payload is
# counted for the client and for the ops series, never silently dropped.
# meiva:link gateway/events/docs/sse.meiva.okf.md
from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter, ValidationError

from base import telemetry
from base.config import settings
from base.events.live.bus import EventBus
from base.events.live.projection import EVENT_ADAPTER, GLOBAL_ROLES
from base.events.live.redis_client import retry_auth_failures_async
from gateway.events.sse import (
    REDIS_ACL_ERRORS,
    REDIS_IO_ERRORS,
    close_pubsub,
    sse_frame,
    warm_frame,
)
from gateway.http.middleware import runtime_metrics, stopping
from ops.alerts import ALERTS_CHANNEL, AlertRow

router = APIRouter()
_log = logging.getLogger(__name__)

Channel = Literal["global", "all", "alerts"]
CHANNELS: tuple[Channel, ...] = ("global", "all", "alerts")

_EVENT_CHANNELS: frozenset[Channel] = frozenset({"global", "all"})

_EVENTS_SOURCE_OWNER: Channel = "all"

_DROP_REPORT_INTERVAL_S = 10.0

_alert_frame_validator = TypeAdapter(AlertRow)


def parse_channels(raw: str | None) -> tuple[Channel, ...]:
    if raw is None:
        return CHANNELS
    requested: list[Channel] = []
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        if name not in CHANNELS:
            raise HTTPException(
                status_code=422,
                detail=f"channels must be a comma-separated subset of {list(CHANNELS)}",
            )
        if name not in requested:
            requested.append(name)
    if not requested:
        raise HTTPException(status_code=422, detail="channels must name at least one channel")
    return tuple(requested)


def parse_agents(raw: str | None) -> set[int] | None:
    if raw is None:
        return None
    try:
        return {int(token) for token in raw.split(",")}
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="agents must be comma-separated integers"
        ) from exc


def merged_frame(channel: Channel, events: Sequence[str], dropped: int = 0) -> bytes:
    body = f'{{"ch":"{channel}","events":[' + ",".join(events) + "]}"
    if dropped:
        body = body[:-1] + f',"dropped":{dropped}}}'
    return sse_frame(body)


def channel_error_frame(channel: Channel, message: str) -> bytes:
    return sse_frame(json.dumps({"ch": channel, "error": message}))


# meiva:note
# One channel's pending payloads between two flushes, capped. A full queue sheds
# its OLDEST payload (the agent-side publisher's own policy) so the newest state
# survives, and counts every shed or unusable payload for the frame and for the
# `sse_drop` report.
# meiva:link gateway/events/docs/sse.meiva.okf.md
class ChannelQueue:
    def __init__(self, channel: Channel, limit: int) -> None:
        self.channel = channel
        self.limit = limit
        self.pending: list[str] = []
        self.dropped = 0
        self.shed = 0
        self.unreported = 0
        self.reported_at = -math.inf

    def add(self, payload: str) -> None:
        if len(self.pending) >= self.limit:
            del self.pending[0]
            self._note_drop()
        self.pending.append(payload)

    def add_unparseable(self) -> None:
        self._note_drop()

    def _note_drop(self) -> None:
        self.dropped += 1
        self.shed += 1
        self.unreported += 1

    def take(self) -> tuple[list[str], int]:
        events, self.pending = self.pending, []
        shed, self.shed = self.shed, 0
        return events, shed

    def report_due(self, now: float) -> int:
        if not self.unreported or now - self.reported_at < _DROP_REPORT_INTERVAL_S:
            return 0
        return self._take_unreported(now)

    def report_remaining(self, now: float) -> int:
        return self._take_unreported(now)

    def _take_unreported(self, now: float) -> int:
        self.reported_at = now
        n = self.unreported
        self.unreported = 0
        return n


Reporter = Callable[[ChannelQueue, int], None]
PubsubPair = tuple[Any, Any]
OpenPubsub = Callable[[EventBus], PubsubPair]


def _report_drops(queue: ChannelQueue, n: int) -> None:
    if not n:
        return
    _log.warning(
        "sse merged channel shed payloads: channel=%s reported=%d total=%d limit=%d",
        queue.channel,
        n,
        queue.dropped,
        queue.limit,
    )
    try:
        telemetry.emit("telemetry", "sse_drop", attributes={"kind": "queue_full", "n": n})
    except RuntimeError:
        _log.warning("sse merged drop report failed: channel=%s n=%d", queue.channel, n)


async def _subscribe(
    pubsub: Any, source: str, served: Sequence[Channel], errors: dict[Channel, str]
) -> None:
    try:
        await retry_auth_failures_async(lambda: pubsub.subscribe(source))
    except REDIS_IO_ERRORS + REDIS_ACL_ERRORS as exc:
        _log.warning("sse merged subscribe failed: source=%s %r", source, exc)
        message = f"event stream interrupted: {type(exc).__name__}"
        for channel in served:
            errors[channel] = message


def open_pubsub(bus: EventBus) -> PubsubPair:
    client: Any = bus.open_async_redis()
    pubsub: Any = client.pubsub()
    return pubsub, client


def _route(
    payload: str,
    source: str,
    queues: dict[Channel, ChannelQueue],
    agent_filter: set[int] | None,
) -> None:
    if source == ALERTS_CHANNEL:
        try:
            _alert_frame_validator.validate_json(payload)
        except ValidationError:
            queues["alerts"].add_unparseable()
            return
        queues["alerts"].add(payload)
        return

    try:
        event = EVENT_ADAPTER.validate_json(payload)
    except ValidationError:
        queues[_EVENTS_SOURCE_OWNER].add_unparseable()
        return
    if agent_filter is not None and event.agent_id != 0 and event.agent_id not in agent_filter:
        return
    if "all" in queues:
        queues["all"].add(payload)
    if "global" in queues and event.role in GLOBAL_ROLES:
        queues["global"].add(payload)


async def _drain(
    pubsub: Any,
    request: Request,
    flush_interval: float,
    queues: dict[Channel, ChannelQueue],
    agent_filter: set[int] | None,
) -> bool:
    if stopping.is_stopping() or await request.is_disconnected():
        return True
    deadline = time.monotonic() + flush_interval
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        poll = min(remaining, 0.1)
        msg = await retry_auth_failures_async(
            lambda poll=poll: pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=poll,
            )
        )
        if msg is None:
            break
        payload = msg["data"]
        source = msg["channel"]
        if not isinstance(payload, str) or not isinstance(source, str):
            continue
        _route(payload, source, queues, agent_filter)
    return False


async def attach_sources(
    bus: EventBus, pubsub: Any, channels: Sequence[Channel]
) -> tuple[list[tuple[str, list[Channel]]], dict[Channel, str]]:
    event_channels: list[Channel] = [channel for channel in channels if channel in _EVENT_CHANNELS]
    alert_channels: list[Channel] = [channel for channel in channels if channel == "alerts"]
    sources: list[tuple[str, list[Channel]]] = []
    if event_channels:
        sources.append((bus.channel, event_channels))
    if alert_channels:
        sources.append((ALERTS_CHANNEL, alert_channels))
    errors: dict[Channel, str] = {}
    for source, served in sources:
        await _subscribe(pubsub, source, served, errors)
    return sources, errors


def flush_frames(
    queues: dict[Channel, ChannelQueue], now: float, reporter: Reporter
) -> tuple[list[bytes], int]:
    frames: list[bytes] = []
    events_sent = 0
    for channel, queue in queues.items():
        events, dropped = queue.take()
        if events:
            frames.append(merged_frame(channel, events, dropped))
            events_sent += len(events)
        reporter(queue, queue.report_due(now))
    return frames, events_sent


# meiva:note
# Flushes every merged channel once per throttle window, so streaming deltas keep
# the batch endpoint's cadence. Lifetime defenses mirror `event_stream`:
# subscription failure is a frame, not a crash; the yields sit inside the try so a
# client abort still closes the pubsub socket; `stopping` ends a gateway handover.
# meiva:link gateway/events/docs/sse.meiva.okf.md
async def _finish(
    pubsub: Any,
    client: Any,
    sources: Sequence[tuple[str, list[Channel]]],
    queues: dict[Channel, ChannelQueue],
    reporter: Reporter,
    opened: float,
    data_frames: int,
    total_events: int,
) -> None:
    for queue in queues.values():
        reporter(queue, queue.report_remaining(time.monotonic()))
    _log.info(
        "sse merged detach: duration=%.1fs data_frames=%d total_events=%d dropped=%s",
        time.monotonic() - opened,
        data_frames,
        total_events,
        {channel: queue.dropped for channel, queue in queues.items()},
    )
    await close_pubsub(pubsub, client, [source for source, _ in sources])


async def merged_event_stream(
    bus: EventBus,
    request: Request,
    *,
    channels: Sequence[Channel],
    agent_filter: set[int] | None,
    throttle_rate: float,
    pending_limit: int,
    reporter: Reporter = _report_drops,
    open_connection: OpenPubsub = open_pubsub,
) -> AsyncGenerator[bytes, None]:
    pubsub, client = open_connection(bus)
    queues: dict[Channel, ChannelQueue] = {
        channel: ChannelQueue(channel, pending_limit) for channel in channels
    }
    opened = time.monotonic()
    data_frames = 0
    total_events = 0
    metrics_opened = False
    flush_interval = 1.0 / throttle_rate
    _log.info(
        "sse merged attach: channels=%s throttle_rate=%.1f/s pending_limit=%d",
        list(channels),
        throttle_rate,
        pending_limit,
    )
    sources: list[tuple[str, list[Channel]]] = []
    try:
        sources, errors = await attach_sources(bus, pubsub, channels)
        for channel, message in errors.items():
            yield channel_error_frame(channel, message)
        if len(errors) == len(channels):
            return
        runtime_metrics.sse_opened("merged")
        metrics_opened = True

        yield b": stream open\n\n"

        last_data_frame = time.monotonic()
        last_comment = last_data_frame
        while True:
            try:
                disconnected = await _drain(pubsub, request, flush_interval, queues, agent_filter)
            except REDIS_IO_ERRORS + REDIS_ACL_ERRORS as exc:
                _log.warning("sse merged pubsub read failed: %r", exc)
                message = f"event stream interrupted: {type(exc).__name__}"
                for channel in queues:
                    yield channel_error_frame(channel, message)
                return
            if disconnected:
                return

            now = time.monotonic()
            frames, events_sent = flush_frames(queues, now, reporter)
            if frames:
                for frame in frames:
                    yield frame
                data_frames += len(frames)
                total_events += events_sent
                last_data_frame = now
                last_comment = now
                continue

            frame, last_comment, last_data_frame = warm_frame(now, last_comment, last_data_frame)
            if frame is not None:
                yield frame
    finally:
        if metrics_opened:
            runtime_metrics.sse_closed("merged")
        await _finish(pubsub, client, sources, queues, reporter, opened, data_frames, total_events)


# meiva:note
# `channels` defaults to every channel and refuses an empty or unknown selection;
# `agents` defaults to no filter, and `agents=0` keeps system-level events only.
# Each flush is `data: {"ch": .., "events": [..]}` plus `"dropped"` when that
# channel shed payloads since its previous flush.
# meiva:link gateway/events/docs/sse.meiva.okf.md
@router.get("/api/stream")
async def get_merged_stream(
    request: Request,
    agents: str | None = None,
    channels: str | None = None,
) -> StreamingResponse:
    return StreamingResponse(
        merged_event_stream(
            request.app.state.bus,
            request,
            channels=parse_channels(channels),
            agent_filter=parse_agents(agents),
            throttle_rate=settings.gateway.sse_throttle_rate,
            pending_limit=settings.gateway.sse_channel_pending_limit,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
